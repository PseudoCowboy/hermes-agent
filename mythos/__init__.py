"""Mythos: Discord-based multi-agent development orchestrator.

Coordinates Athena (main), Prometheus (draft), Argus (review), Hephaestus (test),
Apollo (frontend), and Atlas (backend) across isolated Discord channels.
"""

from mythos.config import MythosConfig, load_config
from mythos.models import (
    AgentRole,
    ApprovalDecision,
    Project,
    ProjectState,
    ReviewComment,
    Spec,
    Workstream,
    WorkstreamType,
)
from mythos.orchestrator import Orchestrator
from mythos.store import ProjectStore

__all__ = [
    "AgentRole",
    "ApprovalDecision",
    "MythosConfig",
    "Orchestrator",
    "Project",
    "ProjectState",
    "ProjectStore",
    "ReviewComment",
    "Spec",
    "Workstream",
    "WorkstreamType",
    "load_config",
]
