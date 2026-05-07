"""Shared pytest fixtures for the Mythos test suite."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict

import pytest

from mythos.agents import AgentInput, AgentOutput, StubAgent
from mythos.config import MythosConfig
from mythos.discord_io import InMemoryDiscordIO
from mythos.orchestrator import MythosOrchestrator
from mythos.project_manager import ProjectManager
from mythos.roles import ROLE_BINDINGS, Role
from mythos.supervisor import Supervisor


# A canned-response map keyed by role. Tests can mutate this before
# triggering inject_user_message to control what each agent "says".
@pytest.fixture
def canned_responses(tmp_path: Path) -> Dict[Role, str]:
    return {
        Role.PROMETHEUS: (
            "# Translator Extension Spec\n\n"
            "## Goals\nA Chrome extension that translates words on double-click.\n\n"
            "## Scope\n- In: dictionary lookup, popup UI.\n- Out: cloud sync.\n\n"
            "## Key Components\n- content script\n- background worker\n- popup UI\n\n"
            "## Open Questions\n- None\n"
        ),
        Role.ARGUS: (
            "## Verdict\nVerdict: APPROVED\n\n"
            "## Comments\n- Looks reasonable.\n- Consider performance of dictionary lookup.\n"
        ),
        Role.ATHENA: (
            "## Frontend (Apollo)\n"
            "Goal: Build the popup + content script.\n"
            "### Inputs\nApproved spec.\n### Outputs\npopup.html, content.js\n"
            "### Acceptance Criteria\nDouble-click triggers lookup.\n\n"
            "## Backend (Atlas)\n"
            "Goal: Embed dictionary + lookup API.\n"
            "### Inputs\nApproved spec.\n### Outputs\ndictionary.json, lookup.js\n"
            "### Acceptance Criteria\nLookup returns within 50ms.\n\n"
            "## Test (Hephaestus)\n"
            "Goal: Cover lookup + UI.\n"
            "### Inputs\nApproved spec.\n### Outputs\ntest_lookup.js\n"
            "### Acceptance Criteria\nAll tests pass.\n"
        ),
        Role.APOLLO: "Built popup.html and content.js. Frontend complete.",
        Role.ATLAS: "Built dictionary.json and lookup.js. Backend complete.",
        Role.HEPHAESTUS: "Wrote test_lookup.js with 12 cases. All passing.",
    }


@pytest.fixture
def cfg(tmp_path: Path) -> MythosConfig:
    cfg = MythosConfig()
    cfg.workspace_root = tmp_path / "ws"
    cfg.workspace_root.mkdir(parents=True, exist_ok=True)
    cfg.discord_token = "test-token"
    cfg.discord_guild_id = 1
    cfg.main_channel_id = 100
    cfg.role_bindings = dict(ROLE_BINDINGS)
    return cfg


@pytest.fixture
def discord() -> InMemoryDiscordIO:
    io = InMemoryDiscordIO()
    return io


@pytest.fixture
def supervisor(cfg: MythosConfig, canned_responses: Dict[Role, str]) -> Supervisor:
    """A Supervisor that hands out StubAgents driven by ``canned_responses``."""

    def factory(role: Role, binding):
        def respond(req: AgentInput) -> AgentOutput:
            text = canned_responses.get(role, f"({role.value} responded)")
            return AgentOutput(role=role, text=text)

        return StubAgent(role=role, response_fn=respond)

    return Supervisor(config=cfg, factory=factory)


@pytest.fixture
def orchestrator(
    cfg: MythosConfig, discord: InMemoryDiscordIO, supervisor: Supervisor
) -> MythosOrchestrator:
    pm = ProjectManager(cfg, discord)
    orch = MythosOrchestrator(cfg, discord, pm, supervisor)
    orch.install_handlers()
    return orch
