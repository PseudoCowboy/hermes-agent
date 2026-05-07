"""Scripted fake CLI runner for mythos integration tests.

Returns canned outputs based on the agent kind and a small content match
on the prompt. Designed to drive the orchestrator through the happy path
without invoking real LLMs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from mythos.cli_runner import CLIResult
from mythos.config import AgentCLIConfig


# Canned spec returned by Prometheus for the chrome-translator project.
TRANSLATOR_SPEC = """\
Overview
--------
Chrome extension that, on double-click in any web page, looks up the
selected word in a built-in dictionary and shows the translation in a
small overlay.

Goals
-----
- Trigger on double-click.
- Use a packaged JSON dictionary (no network).
- Render translation in a non-intrusive overlay.

Non-Goals
---------
- Server-side translation API.
- Multi-language switching in v1.

User Flows
----------
1. User double-clicks a word.
2. Extension reads the selection, looks it up, shows the overlay.

Components
----------
Frontend: content script that captures dblclick, popup overlay UI.
Backend: build script that bundles the JSON dictionary into the extension.
Test: jest unit tests for the lookup helper, and a Playwright smoke test.

Open Questions
--------------
- Default source/target language pair for v1?
"""


REVIEW_TEXT = """\
Strengths
---------
- Clear scope, single trigger event, no network dependency.
- Discipline split is sensible.

Gaps
----
- Dictionary source format is not pinned.
- Edge case: selection across element boundaries.

Risks
-----
- Performance for large dictionaries on page load.

Recommended Changes
-------------------
- Document dictionary file format in v1 spec.

RECOMMENDATION: APPROVE
"""


class ScriptedRunner:
    """Stateful fake runner. Tracks calls per agent."""

    def __init__(self):
        self.calls: List[Dict] = []
        # agent kind -> sequence of canned outputs
        self.scripts: Dict[str, List[str]] = {}

    def queue(self, agent_kind_or_role: str, *outputs: str) -> None:
        self.scripts.setdefault(agent_kind_or_role, []).extend(outputs)

    async def __call__(self, cli: AgentCLIConfig, prompt: str, cwd: Path) -> CLIResult:
        self.calls.append({"kind": cli.kind, "prompt": prompt, "cwd": str(cwd)})
        # Default scripted behavior matched on prompt content.
        # 1. If we have an explicit per-kind queue, pop from it.
        if cli.kind in self.scripts and self.scripts[cli.kind]:
            text = self.scripts[cli.kind].pop(0)
            return CLIResult(ok=True, stdout=text, stderr="", returncode=0,
                             command=" ".join(cli.command))

        # 2. Default behavior per kind.
        if cli.kind == "claude":
            # Claude is used by Athena, Prometheus, Atlas. Distinguish by prompt.
            if "Draft Plan Agent" in prompt or "QUESTION:" in prompt or "produce the full design spec" in prompt or "Produce a revised spec" in prompt:
                # Prometheus
                if "translator" in prompt.lower() and "context" not in prompt.lower() and "user added context" not in prompt.lower() and "vague" in prompt.lower():
                    text = "QUESTION: which language pair should the v1 dictionary support?"
                else:
                    text = TRANSLATOR_SPEC
            elif "Backend Agent" in prompt:
                text = "Backend implementation done.\nBACKEND WORK COMPLETE"
            else:
                text = "Athena ack."
            return CLIResult(ok=True, stdout=text, stderr="", returncode=0,
                             command=" ".join(cli.command))

        if cli.kind == "codex":
            # Codex: Argus or Hephaestus
            if "Review Agent" in prompt or "SPEC:" in prompt:
                text = REVIEW_TEXT
            elif "Test Agent" in prompt or "test" in prompt.lower():
                text = "Tests planned and stubbed.\nTEST WORK COMPLETE"
            else:
                text = "ok"
            return CLIResult(ok=True, stdout=text, stderr="", returncode=0,
                             command=" ".join(cli.command))

        if cli.kind == "gemini":
            text = "Frontend scaffold created.\nFRONTEND WORK COMPLETE"
            return CLIResult(ok=True, stdout=text, stderr="", returncode=0,
                             command=" ".join(cli.command))

        return CLIResult(ok=True, stdout="", stderr="", returncode=0,
                         command=" ".join(cli.command))
