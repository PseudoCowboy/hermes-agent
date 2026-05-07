"""Agent definitions.

Each agent is a small object that:
  - knows its name (e.g. "athena"), CLI config, and channel scope rule
  - exposes `run(prompt, working_dir) -> CLIResult` that shells out to the CLI
  - knows how to format its system prompt for the role

Confinement is enforced by the orchestrator (which decides which agent
handles which channel), not by the agent itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..cli_runner import CLIResult, Runner, run_cli
from ..config import AgentCLIConfig


# Mythological codename -> human-friendly role name.
ROLE_NAMES = {
    "athena": "Main Agent",
    "prometheus": "Draft Plan Agent",
    "argus": "Review Agent",
    "hephaestus": "Test Agent",
    "apollo": "Frontend Agent",
    "atlas": "Backend Agent",
}

# What discipline channel each specialist owns. Specialists ONLY respond
# in their assigned channel (per-channel agent confinement).
SPECIALIST_DISCIPLINE = {
    "apollo": "frontend",
    "atlas": "backend",
    "hephaestus": "test",
}


@dataclass
class Agent:
    name: str  # codename, e.g. "athena"
    cli: AgentCLIConfig
    role_name: str
    system_preamble: str
    runner: Runner = run_cli  # injectable for tests

    async def run(self, user_prompt: str, working_dir: Path) -> CLIResult:
        prompt = f"{self.system_preamble}\n\n---\n\n{user_prompt}"
        return await self.runner(self.cli, prompt, working_dir)


# ---------------------------------------------------------------------------
# Factory: build the six-agent roster from a config.
# ---------------------------------------------------------------------------

def build_roster(agent_cli, runner: Runner = run_cli):
    """Return dict[name -> Agent]. `agent_cli` is dict[name -> AgentCLIConfig]."""
    return {
        "athena": Agent(
            name="athena", cli=agent_cli["athena"], role_name=ROLE_NAMES["athena"],
            system_preamble=ATHENA_PREAMBLE, runner=runner,
        ),
        "prometheus": Agent(
            name="prometheus", cli=agent_cli["prometheus"], role_name=ROLE_NAMES["prometheus"],
            system_preamble=PROMETHEUS_PREAMBLE, runner=runner,
        ),
        "argus": Agent(
            name="argus", cli=agent_cli["argus"], role_name=ROLE_NAMES["argus"],
            system_preamble=ARGUS_PREAMBLE, runner=runner,
        ),
        "hephaestus": Agent(
            name="hephaestus", cli=agent_cli["hephaestus"], role_name=ROLE_NAMES["hephaestus"],
            system_preamble=HEPHAESTUS_PREAMBLE, runner=runner,
        ),
        "apollo": Agent(
            name="apollo", cli=agent_cli["apollo"], role_name=ROLE_NAMES["apollo"],
            system_preamble=APOLLO_PREAMBLE, runner=runner,
        ),
        "atlas": Agent(
            name="atlas", cli=agent_cli["atlas"], role_name=ROLE_NAMES["atlas"],
            system_preamble=ATLAS_PREAMBLE, runner=runner,
        ),
    }


# ---------------------------------------------------------------------------
# Per-role system preambles. Kept short — the orchestrator passes the
# task-specific instruction as the user prompt.
# ---------------------------------------------------------------------------

ATHENA_PREAMBLE = """You are Athena, the Main Agent of a Discord-based multi-agent
development system. You receive free-form project requests in the main channel,
acknowledge them concisely, and coordinate handoffs to other agents. Keep replies
short and structured — they are posted into a Discord channel."""

PROMETHEUS_PREAMBLE = """You are Prometheus, the Draft Plan Agent. Given a user's
project idea, you either ask up to 3 focused clarifying questions OR produce a
structured design specification. Output sections: Overview, Goals, Non-Goals,
User Flows, Components (frontend/backend/test as applicable), Open Questions.
Keep total length under 1200 words."""

ARGUS_PREAMBLE = """You are Argus, the Review Agent. Given a draft design spec
produced by Prometheus, post a structured review with: Strengths, Gaps, Risks,
Recommended Changes. End with one of: 'RECOMMENDATION: APPROVE',
'RECOMMENDATION: REVISE', 'RECOMMENDATION: REJECT'."""

HEPHAESTUS_PREAMBLE = """You are Hephaestus, the Test Agent. Given an approved
design spec and a list of test work items, produce test plans and test code in
your assigned discipline channel only. Post a final 'TEST WORK COMPLETE' line
when done."""

APOLLO_PREAMBLE = """You are Apollo, the Frontend Agent. Given an approved design
spec and the frontend work items, implement the frontend in your assigned
discipline channel only. Ask clarifying questions only in this channel. Post a
final 'FRONTEND WORK COMPLETE' line when done."""

ATLAS_PREAMBLE = """You are Atlas, the Backend Agent. Given an approved design
spec and the backend work items, implement the backend in your assigned
discipline channel only. Ask clarifying questions only in this channel. Post a
final 'BACKEND WORK COMPLETE' line when done."""
