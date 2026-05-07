"""The agent roster is fixed by the user-scenario. These tests act as a
regression guard against accidental changes to (role → name → backend).
"""

from __future__ import annotations

import pytest

from mythos.agents import AGENT_ROSTER, AgentBackend, AgentRole


@pytest.mark.parametrize(
    "role,expected_name,expected_backend",
    [
        (AgentRole.MAIN, "Athena", AgentBackend.CLAUDE_CODE),
        (AgentRole.DRAFT_PLAN, "Prometheus", AgentBackend.CLAUDE_CODE),
        (AgentRole.REVIEW, "Argus", AgentBackend.CODEX),
        (AgentRole.TEST, "Hephaestus", AgentBackend.CODEX),
        (AgentRole.FRONTEND, "Apollo", AgentBackend.GEMINI),
        (AgentRole.BACKEND, "Atlas", AgentBackend.CLAUDE_CODE),
    ],
)
def test_roster_is_fixed(role, expected_name, expected_backend):
    spec = AGENT_ROSTER[role]
    assert spec.name == expected_name
    assert spec.backend is expected_backend


def test_roster_has_no_extras():
    assert set(AGENT_ROSTER) == set(AgentRole)
