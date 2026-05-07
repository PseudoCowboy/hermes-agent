"""Mythos: Discord-based multi-agent development orchestrator.

Built on top of hermes-agent's Discord gateway. See mythos/SETUP.md.
"""

from mythos.config import MythosConfig, AgentConfig, load_config
from mythos.state import (
    Project,
    ProjectState,
    Workstream,
    WorkstreamKind,
    DesignSpec,
    ReviewComments,
)
from mythos.orchestrator import Orchestrator
from mythos.agents import AgentRole, AGENT_ROSTER

__all__ = [
    "MythosConfig",
    "AgentConfig",
    "load_config",
    "Project",
    "ProjectState",
    "Workstream",
    "WorkstreamKind",
    "DesignSpec",
    "ReviewComments",
    "Orchestrator",
    "AgentRole",
    "AGENT_ROSTER",
]
