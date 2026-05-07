"""Mythos: Discord-native multi-agent development orchestration system.

A deterministic orchestrator that turns a Discord message into a planned,
reviewed, and implemented software project, dispatching specialist
CLI-backed agents (Claude / Codex / Gemini) into per-project channels.
"""

from mythos.config import MythosConfig, load_config
from mythos.models import (
    AgentRole,
    Project,
    ProjectChannel,
    ProjectPhase,
    Task,
)
from mythos.orchestrator import Orchestrator
from mythos.state_machine import ALLOWED_TRANSITIONS, can_transition

__all__ = [
    "MythosConfig",
    "load_config",
    "AgentRole",
    "Project",
    "ProjectChannel",
    "ProjectPhase",
    "Task",
    "Orchestrator",
    "ALLOWED_TRANSITIONS",
    "can_transition",
]

__version__ = "0.1.0"
