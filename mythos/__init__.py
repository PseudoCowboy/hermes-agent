"""Mythos: a Discord-native multi-agent development orchestration system.

Built on top of hermes-agent's Discord gateway and subprocess delegation
primitives.  See ``mythos/SETUP.md`` for operator instructions.
"""

from mythos.config import MythosConfig, load_config
from mythos.projects import Project, ProjectStatus, ProjectStore
from mythos.orchestrator import Orchestrator
from mythos.router import MessageRouter

__all__ = [
    "MythosConfig",
    "load_config",
    "Project",
    "ProjectStatus",
    "ProjectStore",
    "Orchestrator",
    "MessageRouter",
]
