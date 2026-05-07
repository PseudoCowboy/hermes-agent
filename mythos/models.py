"""Domain models for Mythos.

Mirrors the entities defined in the spec: Project, Channel Link, Agent Role,
Agent Run, Design Spec, Review Comment, Approval Decision, Workstream, Artifact.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ProjectState(str, Enum):
    INTAKE = "intake"
    CLARIFYING = "clarifying"
    DRAFTING = "drafting"
    REVIEWING = "reviewing"
    AWAITING_APPROVAL = "awaiting_approval"
    DECOMPOSING = "decomposing"
    IMPLEMENTING = "implementing"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    ARCHIVED = "archived"
    INTAKE_FAILED = "intake_failed"


class AgentRole(str, Enum):
    ATHENA = "athena"  # Main
    PROMETHEUS = "prometheus"  # Draft plan
    ARGUS = "argus"  # Review
    HEPHAESTUS = "hephaestus"  # Test
    APOLLO = "apollo"  # Frontend
    ATLAS = "atlas"  # Backend


class WorkstreamType(str, Enum):
    FRONTEND = "frontend"
    BACKEND = "backend"
    TEST = "test"


# Map a workstream type to the agent role that owns it.
WORKSTREAM_AGENT: dict[WorkstreamType, AgentRole] = {
    WorkstreamType.FRONTEND: AgentRole.APOLLO,
    WorkstreamType.BACKEND: AgentRole.ATLAS,
    WorkstreamType.TEST: AgentRole.HEPHAESTUS,
}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass
class Spec:
    project_id: str
    version: int
    content: str
    author_role: AgentRole = AgentRole.PROMETHEUS
    created_at: float = field(default_factory=time.time)
    review_status: str = "pending"  # pending | acceptable | needs_changes
    approval_status: str = "pending"  # pending | approved | rejected | revised


@dataclass
class ReviewComment:
    spec_version: int
    reviewer: AgentRole
    severity: str  # info | minor | major | blocker
    text: str
    recommendation: str  # accept | request_changes
    created_at: float = field(default_factory=time.time)


@dataclass
class ApprovalDecision:
    spec_version: int
    approver: str  # user discord id or display name
    decision: str  # approved | rejected | revise
    requested_changes: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class Workstream:
    project_id: str
    type: WorkstreamType
    role: AgentRole
    scope: str
    channel_id: int | None = None
    channel_name: str = ""
    status: str = "pending"  # pending | in_progress | completed | blocked
    blocker: str = ""
    artifacts: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: _new_id("ws"))


@dataclass
class AgentRun:
    project_id: str
    role: AgentRole
    workstream_id: str | None = None
    status: str = "pending"  # pending | running | completed | failed | blocked
    summary: str = ""
    blocker_reason: str = ""
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    output: str = ""
    id: str = field(default_factory=lambda: _new_id("run"))


@dataclass
class Artifact:
    project_id: str
    artifact_type: str  # spec | review | impl | test | log
    path: str
    producer: AgentRole
    workstream_id: str | None = None
    version: int = 1
    created_at: float = field(default_factory=time.time)


@dataclass
class Project:
    """A user-requested development effort. Each project is isolated."""

    display_name: str
    requester: str
    source_message_id: str
    request_text: str
    state: ProjectState = ProjectState.INTAKE
    project_channel_id: int | None = None
    project_channel_name: str = ""
    workspace_dir: str = ""
    specs: list[Spec] = field(default_factory=list)
    review_comments: list[ReviewComment] = field(default_factory=list)
    approvals: list[ApprovalDecision] = field(default_factory=list)
    workstreams: dict[str, Workstream] = field(default_factory=dict)
    runs: list[AgentRun] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    state_history: list[tuple[float, ProjectState]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    archived_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: _new_id("proj"))

    def latest_spec(self) -> Spec | None:
        return self.specs[-1] if self.specs else None

    def latest_review(self) -> ReviewComment | None:
        return self.review_comments[-1] if self.review_comments else None

    def latest_approval(self) -> ApprovalDecision | None:
        return self.approvals[-1] if self.approvals else None
