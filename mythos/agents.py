"""Agent role implementations.

Each agent role wraps the CLI runtime with a role-specific prompt template and
post-processing. They are deliberately thin — most coordination logic lives in
``mythos.orchestrator``. Each role owns:

* A ``role`` enum value and a human display name.
* A ``run_*`` method that generates the appropriate prompt for its phase of the
  workflow and returns a structured ``RoleOutput``.
* Channel-confinement metadata used by the Discord bridge to refuse messages
  from agents posting in the wrong channel.

Per the spec (FR-022) specialist agents must only post in their assigned
workstream channels. The orchestrator enforces this by passing each agent the
specific channel id it owns; if a role somehow attempts to post elsewhere, the
bridge logs an error and redirects.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mythos.cli_runtime import CLIResult, CLIRuntime
from mythos.models import AgentRole, WorkstreamType


@dataclass
class RoleOutput:
    role: AgentRole
    text: str
    raw: CLIResult
    structured: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.raw.ok


def _safe_json_block(text: str) -> dict[str, Any] | None:
    """Find the first ``{...}`` JSON object in ``text`` and parse it."""

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    # Greedy scan for the first `{` and matching `}`.
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None


# --- Athena (main) ----------------------------------------------------------


ATHENA_INTAKE_PROMPT = """You are Athena, the main coordinator of the Mythos Discord
multi-agent development system. A user just posted a project request in the
main channel.

Project request:
\"\"\"
{request}
\"\"\"

Produce a JSON object with this exact shape:
{{
  "display_name": "<short kebab- or title-case project name, max 6 words>",
  "summary": "<one-sentence summary of the user's intent>",
  "intake_message": "<warm, concise message you will post in the main channel acknowledging the request and naming the new project channel>"
}}
Only output the JSON object.
"""


ATHENA_DECOMPOSE_PROMPT = """You are Athena, the main coordinator. The user has just
APPROVED the design spec below. Decompose the work into specialist workstreams
and choose which are needed.

Approved spec (v{spec_version}):
\"\"\"
{spec}
\"\"\"

Choose any subset of [frontend, backend, test]. Always include 'test' unless
the spec is purely documentation. Always include 'frontend' if the spec
mentions UI/UX/web/extension/mobile/CLI front-end. Always include 'backend' if
the spec mentions an API, server, database, agent, or background processing.

Return JSON:
{{
  "workstreams": [
    {{"type": "frontend"|"backend"|"test", "scope": "<2-4 sentence scope summary>"}}
  ],
  "channel_message": "<message to post in the project channel announcing the decomposition>"
}}
Only output JSON.
"""


# --- Prometheus (draft plan) -----------------------------------------------


PROMETHEUS_DRAFT_PROMPT = """You are Prometheus, the draft plan agent. You work in
the project channel for the project below. Decide whether you need to ask
clarifying questions before writing a design spec.

Project request:
\"\"\"
{request}
\"\"\"

Prior clarification Q&A (most recent last; may be empty):
{clarifications}

Return JSON:
{{
  "needs_clarification": true|false,
  "clarifying_questions": ["<question 1>", ...],   // present iff needs_clarification
  "spec_markdown": "<full markdown design spec>",   // present iff !needs_clarification
  "channel_message": "<message you will post in the project channel>"
}}

Rules: ask 1-3 questions only when something blocks producing a useful spec.
The spec should cover: goals, user stories, key requirements, suggested
architecture, frontend scope, backend scope, test scope, open questions.
Only output JSON.
"""


PROMETHEUS_REVISE_PROMPT = """You are Prometheus. The reviewer (Argus) and the
user requested changes to your previous spec. Produce a revised spec.

Previous spec (v{prev_version}):
\"\"\"
{prev_spec}
\"\"\"

Review comments:
\"\"\"
{review}
\"\"\"

User requested changes:
\"\"\"
{user_changes}
\"\"\"

Return JSON:
{{
  "spec_markdown": "<full revised markdown design spec>",
  "channel_message": "<message announcing the revision>"
}}
Only output JSON.
"""


# --- Argus (review) ---------------------------------------------------------


ARGUS_REVIEW_PROMPT = """You are Argus, the review agent. Critique the design spec
below for completeness, clarity, feasibility, and risk.

Spec (v{spec_version}):
\"\"\"
{spec}
\"\"\"

Return JSON:
{{
  "recommendation": "accept" | "request_changes",
  "severity": "info" | "minor" | "major" | "blocker",
  "review_markdown": "<markdown review with bullet points: strengths, concerns, must-fix>",
  "channel_message": "<concise message to post in the project channel summarizing your review>"
}}
Only output JSON.
"""


# --- Hephaestus (test) ------------------------------------------------------


HEPHAESTUS_PROMPT = """You are Hephaestus, the test agent. The user-approved spec
is below. You own the test workstream and may only operate inside the test
channel.

Approved spec:
\"\"\"
{spec}
\"\"\"

Workstream scope assigned to you:
\"\"\"
{scope}
\"\"\"

Return JSON:
{{
  "test_plan_markdown": "<test plan covering happy path, edge cases, regression>",
  "test_artifact_path": "<relative file path under your workspace, e.g. tests/translator.test.ts>",
  "test_artifact_content": "<contents of that test file>",
  "channel_message": "<message announcing test plan completion in the test channel>"
}}
Only output JSON.
"""


# --- Apollo (frontend, gemini) ---------------------------------------------


APOLLO_PROMPT = """You are Apollo, the frontend agent. You drive the Gemini CLI.
You only post in the frontend channel.

Approved spec:
\"\"\"
{spec}
\"\"\"

Frontend scope assigned to you:
\"\"\"
{scope}
\"\"\"

Return JSON:
{{
  "frontend_plan_markdown": "<frontend implementation plan>",
  "artifact_path": "<relative file path you would create, e.g. src/popup.tsx>",
  "artifact_content": "<source you would write to that file>",
  "channel_message": "<completion message for the frontend channel>"
}}
Only output JSON.
"""


# --- Atlas (backend, claude code) ------------------------------------------


ATLAS_PROMPT = """You are Atlas, the backend agent. You only post in the backend
channel.

Approved spec:
\"\"\"
{spec}
\"\"\"

Backend scope assigned to you:
\"\"\"
{scope}
\"\"\"

Return JSON:
{{
  "backend_plan_markdown": "<backend implementation plan>",
  "artifact_path": "<relative file path you would create>",
  "artifact_content": "<source contents>",
  "channel_message": "<completion message for the backend channel>"
}}
Only output JSON.
"""


# --- BaseAgent --------------------------------------------------------------


class BaseAgent:
    role: AgentRole

    def __init__(self, runtime: CLIRuntime) -> None:
        self.runtime = runtime

    async def _invoke(self, prompt: str, workspace: Path) -> RoleOutput:
        result = await self.runtime.run(self.role, prompt, workspace)
        text = result.stdout.strip() or result.stderr.strip()
        structured = _safe_json_block(text) or {}
        return RoleOutput(role=self.role, text=text, raw=result, structured=structured)


class AthenaAgent(BaseAgent):
    role = AgentRole.ATHENA

    async def intake(self, *, request: str, workspace: Path) -> RoleOutput:
        return await self._invoke(ATHENA_INTAKE_PROMPT.format(request=request), workspace)

    async def decompose(self, *, spec: str, spec_version: int, workspace: Path) -> RoleOutput:
        return await self._invoke(
            ATHENA_DECOMPOSE_PROMPT.format(spec=spec, spec_version=spec_version), workspace
        )


class PrometheusAgent(BaseAgent):
    role = AgentRole.PROMETHEUS

    async def draft(
        self,
        *,
        request: str,
        clarifications: list[tuple[str, str]],
        workspace: Path,
    ) -> RoleOutput:
        if clarifications:
            joined = "\n".join(f"- Q: {q}\n  A: {a}" for q, a in clarifications)
        else:
            joined = "(none yet)"
        return await self._invoke(
            PROMETHEUS_DRAFT_PROMPT.format(request=request, clarifications=joined), workspace
        )

    async def revise(
        self,
        *,
        prev_spec: str,
        prev_version: int,
        review: str,
        user_changes: str,
        workspace: Path,
    ) -> RoleOutput:
        return await self._invoke(
            PROMETHEUS_REVISE_PROMPT.format(
                prev_spec=prev_spec,
                prev_version=prev_version,
                review=review,
                user_changes=user_changes,
            ),
            workspace,
        )


class ArgusAgent(BaseAgent):
    role = AgentRole.ARGUS

    async def review(self, *, spec: str, spec_version: int, workspace: Path) -> RoleOutput:
        return await self._invoke(
            ARGUS_REVIEW_PROMPT.format(spec=spec, spec_version=spec_version), workspace
        )


class HephaestusAgent(BaseAgent):
    role = AgentRole.HEPHAESTUS

    async def implement(self, *, spec: str, scope: str, workspace: Path) -> RoleOutput:
        return await self._invoke(
            HEPHAESTUS_PROMPT.format(spec=spec, scope=scope), workspace
        )


class ApolloAgent(BaseAgent):
    role = AgentRole.APOLLO

    async def implement(self, *, spec: str, scope: str, workspace: Path) -> RoleOutput:
        return await self._invoke(
            APOLLO_PROMPT.format(spec=spec, scope=scope), workspace
        )


class AtlasAgent(BaseAgent):
    role = AgentRole.ATLAS

    async def implement(self, *, spec: str, scope: str, workspace: Path) -> RoleOutput:
        return await self._invoke(
            ATLAS_PROMPT.format(spec=spec, scope=scope), workspace
        )


WORKSTREAM_AGENT_CLASS = {
    WorkstreamType.FRONTEND: ApolloAgent,
    WorkstreamType.BACKEND: AtlasAgent,
    WorkstreamType.TEST: HephaestusAgent,
}
