"""Mythos: a Discord-based multi-agent development orchestrator.

Built on top of hermes-agent's Discord gateway primitives, mythos coordinates
six role-specialized coding agents (Athena, Prometheus, Argus, Hephaestus,
Apollo, Atlas) across per-project Discord channels.

Public entrypoints live in :mod:`mythos.bot` and :mod:`mythos.orchestrator`.
"""

from mythos.config import MythosConfig, load_config
from mythos.state import (
    Project,
    ProjectStatus,
    ChannelBinding,
    AgentTask,
    DesignReview,
    Approval,
    StateStore,
)
from mythos.orchestrator import Orchestrator

__all__ = [
    "MythosConfig",
    "load_config",
    "Project",
    "ProjectStatus",
    "ChannelBinding",
    "AgentTask",
    "DesignReview",
    "Approval",
    "StateStore",
    "Orchestrator",
]
