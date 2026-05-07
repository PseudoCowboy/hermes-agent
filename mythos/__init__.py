"""Mythos: Discord-based multi-agent development orchestration.

Built on top of hermes-agent's Discord gateway. Coordinates a roster of
specialized CLI-driven agents (Claude Code, Codex, Gemini) across isolated
per-project Discord channels.
"""

from mythos.roles import AgentRole, ROLE_REGISTRY
from mythos.state import (
    ProjectPhase,
    ProjectRecord,
    ReviewRound,
    WorkstreamRecord,
    WorkstreamKind,
)

__all__ = [
    "AgentRole",
    "ROLE_REGISTRY",
    "ProjectPhase",
    "ProjectRecord",
    "ReviewRound",
    "WorkstreamRecord",
    "WorkstreamKind",
]
