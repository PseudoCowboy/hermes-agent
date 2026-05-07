"""Mythos data models.

Plain dataclasses describing projects, channels, tasks, artifacts, and
agent runs. Persisted as JSON via mythos.store.
"""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class AgentRole(str, enum.Enum):
    MAIN = "main"               # Athena   (Claude Code)
    DRAFT_PLAN = "draft_plan"   # Prometheus (Claude Code)
    REVIEW = "review"           # Argus    (Codex)
    FRONTEND = "frontend"       # Apollo   (Gemini)
    BACKEND = "backend"         # Atlas    (Claude Code)
    TEST = "test"               # Hephaestus (Codex)


# Stable mythological codenames per role.
ROLE_CODENAME = {
    AgentRole.MAIN: "Athena",
    AgentRole.DRAFT_PLAN: "Prometheus",
    AgentRole.REVIEW: "Argus",
    AgentRole.FRONTEND: "Apollo",
    AgentRole.BACKEND: "Atlas",
    AgentRole.TEST: "Hephaestus",
}


class ChannelRole(str, enum.Enum):
    PROJECT = "project"   # primary planning / approval channel
    FRONTEND = "frontend"
    BACKEND = "backend"
    TEST = "test"


class ProjectPhase(str, enum.Enum):
    INTAKE = "intake"
    PLANNING = "planning"
    CLARIFICATION = "clarification"
    DRAFT_READY = "draft_ready"
    REVIEW = "review"
    REVISION = "revision"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    DECOMPOSING = "decomposing"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ArtifactType(str, enum.Enum):
    REQUEST = "request"
    DESIGN_SPEC = "design_spec"
    REVIEW_COMMENTS = "review_comments"
    APPROVED_DESIGN = "approved_design"
    TASK_BRIEF = "task_brief"
    IMPLEMENTATION_SUMMARY = "implementation_summary"
    TEST_REPORT = "test_report"


@dataclass
class Project:
    id: str = field(default_factory=lambda: _new_id("prj"))
    title: str = ""
    request_text: str = ""
    owner_user_id: str = ""
    source_channel_id: str = ""
    source_message_id: str = ""
    project_channel_id: str = ""
    workspace_path: str = ""
    phase: ProjectPhase = ProjectPhase.INTAKE
    review_round: int = 0
    approved_design_artifact_id: Optional[str] = None
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["phase"] = self.phase.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Project":
        data = dict(data)
        if "phase" in data and not isinstance(data["phase"], ProjectPhase):
            data["phase"] = ProjectPhase(data["phase"])
        return cls(**data)


@dataclass
class ProjectChannel:
    id: str = field(default_factory=lambda: _new_id("chn"))
    project_id: str = ""
    discord_channel_id: str = ""
    channel_role: ChannelRole = ChannelRole.PROJECT
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["channel_role"] = self.channel_role.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProjectChannel":
        data = dict(data)
        if "channel_role" in data and not isinstance(data["channel_role"], ChannelRole):
            data["channel_role"] = ChannelRole(data["channel_role"])
        return cls(**data)


@dataclass
class Artifact:
    id: str = field(default_factory=lambda: _new_id("art"))
    project_id: str = ""
    type: ArtifactType = ArtifactType.REQUEST
    version: int = 1
    path: str = ""
    immutable: bool = False
    discord_message_id: str = ""
    produced_by_agent_run_id: str = ""
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Artifact":
        data = dict(data)
        if "type" in data and not isinstance(data["type"], ArtifactType):
            data["type"] = ArtifactType(data["type"])
        return cls(**data)


@dataclass
class Task:
    id: str = field(default_factory=lambda: _new_id("tsk"))
    project_id: str = ""
    role: AgentRole = AgentRole.BACKEND
    title: str = ""
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    source_artifact_id: str = ""
    target_channel_id: str = ""
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["role"] = self.role.value
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Task":
        data = dict(data)
        if "role" in data and not isinstance(data["role"], AgentRole):
            data["role"] = AgentRole(data["role"])
        if "status" in data and not isinstance(data["status"], TaskStatus):
            data["status"] = TaskStatus(data["status"])
        return cls(**data)


@dataclass
class AgentRun:
    id: str = field(default_factory=lambda: _new_id("run"))
    project_id: str = ""
    task_id: Optional[str] = None
    role: AgentRole = AgentRole.MAIN
    provider: str = ""             # claude | codex | gemini
    command: str = ""
    started_at: float = field(default_factory=_now)
    ended_at: float = 0.0
    exit_code: int = 0
    stdout_path: str = ""
    stderr_path: str = ""
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["role"] = self.role.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentRun":
        data = dict(data)
        if "role" in data and not isinstance(data["role"], AgentRole):
            data["role"] = AgentRole(data["role"])
        return cls(**data)


@dataclass
class Approval:
    id: str = field(default_factory=lambda: _new_id("apr"))
    project_id: str = ""
    artifact_id: str = ""
    approved_by_user_id: str = ""
    approval_message_id: str = ""
    approved_at: float = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Approval":
        return cls(**data)


@dataclass
class AuditEvent:
    id: str = field(default_factory=lambda: _new_id("aud"))
    project_id: str = ""
    event_type: str = ""
    actor_type: str = "system"  # user | system | agent
    actor_id: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AuditEvent":
        return cls(**data)
