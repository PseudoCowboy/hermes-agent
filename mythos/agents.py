"""Agent role definitions and roster for Mythos.

Each agent has a fixed mythological codename and a fixed CLI backend. This
mapping is enforced — see ``tests/mythos/test_agent_roster.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class AgentRole(str, Enum):
    """The fixed agent roles in the Mythos system."""

    MAIN = "main"
    DRAFT_PLAN = "draft_plan"
    REVIEW = "review"
    TEST = "test"
    FRONTEND = "frontend"
    BACKEND = "backend"


class AgentBackend(str, Enum):
    """CLI backends available to agents."""

    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    GEMINI = "gemini"


@dataclass(frozen=True)
class AgentSpec:
    """Static spec for an agent role.

    The (role, name, backend) triple is fixed by the user-scenario; only the
    runtime parameters (model, base url, effort) live in :class:`AgentConfig`.
    """

    role: AgentRole
    name: str  # mythological codename
    backend: AgentBackend
    description: str


AGENT_ROSTER: Dict[AgentRole, AgentSpec] = {
    AgentRole.MAIN: AgentSpec(
        role=AgentRole.MAIN,
        name="Athena",
        backend=AgentBackend.CLAUDE_CODE,
        description="Main agent: intake, coordination, decomposition, approval",
    ),
    AgentRole.DRAFT_PLAN: AgentSpec(
        role=AgentRole.DRAFT_PLAN,
        name="Prometheus",
        backend=AgentBackend.CLAUDE_CODE,
        description="Draft plan agent: clarify, draft design spec",
    ),
    AgentRole.REVIEW: AgentSpec(
        role=AgentRole.REVIEW,
        name="Argus",
        backend=AgentBackend.CODEX,
        description="Review agent: review design specs, post review comments",
    ),
    AgentRole.TEST: AgentSpec(
        role=AgentRole.TEST,
        name="Hephaestus",
        backend=AgentBackend.CODEX,
        description="Test agent: verification and test work",
    ),
    AgentRole.FRONTEND: AgentSpec(
        role=AgentRole.FRONTEND,
        name="Apollo",
        backend=AgentBackend.GEMINI,
        description="Frontend agent: UI / web client implementation",
    ),
    AgentRole.BACKEND: AgentSpec(
        role=AgentRole.BACKEND,
        name="Atlas",
        backend=AgentBackend.CLAUDE_CODE,
        description="Backend agent: server / API implementation",
    ),
}


def role_by_name(name: str) -> Optional[AgentRole]:
    """Look up a role by mythological codename, case-insensitive."""
    needle = name.strip().lower()
    for spec in AGENT_ROSTER.values():
        if spec.name.lower() == needle:
            return spec.role
    return None
