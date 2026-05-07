"""Mythos role and channel constants."""
from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """Agent roles in the mythos system."""

    ATHENA = "athena"          # Main / orchestrator
    PROMETHEUS = "prometheus"  # Draft Plan Agent
    ARGUS = "argus"            # Review Agent
    HEPHAESTUS = "hephaestus"  # Test Agent
    APOLLO = "apollo"          # Frontend Agent
    ATLAS = "atlas"            # Backend Agent

    @property
    def display_name(self) -> str:
        return self.value.capitalize()


class ChannelKind(str, Enum):
    PLAN = "plan"
    FRONTEND = "frontend"
    BACKEND = "backend"
    TEST = "test"


# Mapping of channel kind -> the role allowed to ask questions / post in it.
CHANNEL_OWNERSHIP: dict[ChannelKind, tuple[Role, ...]] = {
    ChannelKind.PLAN: (Role.ATHENA, Role.PROMETHEUS, Role.ARGUS),
    ChannelKind.FRONTEND: (Role.APOLLO,),
    ChannelKind.BACKEND: (Role.ATLAS,),
    ChannelKind.TEST: (Role.HEPHAESTUS,),
}


# Which CLI drives each role.
ROLE_TO_CLI: dict[Role, str] = {
    Role.ATHENA: "claude",
    Role.PROMETHEUS: "claude",
    Role.ATLAS: "claude",
    Role.ARGUS: "codex",
    Role.HEPHAESTUS: "codex",
    Role.APOLLO: "gemini",
}


class ProjectStatus(str, Enum):
    INTAKE_RECEIVED = "intake_received"
    PROJECT_CHANNEL_CREATED = "project_channel_created"
    CLARIFYING_QUESTIONS = "clarifying_questions"
    DESIGN_DRAFTING = "design_drafting"
    DESIGN_REVIEW = "design_review"
    AWAITING_USER_APPROVAL = "awaiting_user_approval"
    REVISION_REQUESTED = "revision_requested"
    APPROVED_FOR_IMPLEMENTATION = "approved_for_implementation"
    WORK_DECOMPOSED = "work_decomposed"
    IMPLEMENTATION_IN_PROGRESS = "implementation_in_progress"
    TESTING_IN_PROGRESS = "testing_in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED_NEEDS_ATTENTION = "failed_needs_attention"
