"""In-memory + JSON-backed state store for Mythos projects.

The store is keyed entirely by project ID — every read and write requires a
project ID, which makes cross-project leakage easy to detect and block at the
boundary (see ``StateStore.assert_channel_in_project``).

Persistence is intentionally simple: each project gets one JSON file under
``state_dir/<project_id>.json``. Atomic replace on save. Concurrent writes
from a single asyncio loop are guarded by an ``asyncio.Lock`` per project.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class ProjectStatus(str, Enum):
    INTAKE = "intake"
    PLANNING = "planning"
    REVIEWING = "reviewing"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    DECOMPOSED = "decomposed"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    DONE = "done"
    FAILED = "failed"


CHANNEL_TYPE_PROJECT = "project"
CHANNEL_TYPE_FRONTEND = "frontend"
CHANNEL_TYPE_BACKEND = "backend"
CHANNEL_TYPE_TEST = "test"


@dataclass
class ChannelBinding:
    project_id: str
    channel_id: int
    channel_type: str
    allowed_roles: List[str]
    lifecycle_status: str = "open"


@dataclass
class DesignVersion:
    version: int
    body: str
    author_role: str
    created_at: float = field(default_factory=_now)


@dataclass
class DesignReview:
    project_id: str
    design_version: int
    review_run_id: str
    comments: str
    decision: str  # "approve_recommended" | "changes_requested" | "blocked"
    created_at: float = field(default_factory=_now)


@dataclass
class Approval:
    project_id: str
    design_version: int
    user_id: int
    decision: str  # "approved" | "rejected"
    created_at: float = field(default_factory=_now)


@dataclass
class AgentTask:
    project_id: str
    task_id: str
    role: str
    provider: str
    channel_id: int
    workspace_path: str
    input_design_version: int
    instructions: str
    status: str = "pending"
    output_summary: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


@dataclass
class Project:
    id: str
    slug: str
    title: str
    source_message_id: int
    owner_user_id: int
    status: ProjectStatus
    workspace_path: str
    project_channel_id: Optional[int] = None
    channels: Dict[str, ChannelBinding] = field(default_factory=dict)  # channel_type -> binding (project) or task_id -> binding
    design_versions: List[DesignVersion] = field(default_factory=list)
    reviews: List[DesignReview] = field(default_factory=list)
    approvals: List[Approval] = field(default_factory=list)
    tasks: Dict[str, AgentTask] = field(default_factory=dict)
    audit_log: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)

    # ------------------------------------------------------------------
    # Convenience accessors

    def latest_design(self) -> Optional[DesignVersion]:
        return self.design_versions[-1] if self.design_versions else None

    def latest_review(self) -> Optional[DesignReview]:
        return self.reviews[-1] if self.reviews else None

    def latest_approval(self) -> Optional[Approval]:
        return self.approvals[-1] if self.approvals else None

    def is_approved(self) -> bool:
        appr = self.latest_approval()
        latest = self.latest_design()
        if not appr or not latest:
            return False
        return (
            appr.decision == "approved"
            and appr.design_version == latest.version
        )

    def channel_belongs_to_project(self, channel_id: int) -> bool:
        if self.project_channel_id == channel_id:
            return True
        for binding in self.channels.values():
            if binding.channel_id == channel_id:
                return True
        return False


class CrossProjectError(RuntimeError):
    """Raised when an agent tries to act on a channel outside its project."""


class StateStore:
    """Async-friendly project state store with per-project JSON persistence."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._projects: Dict[str, Project] = {}
        self._channel_index: Dict[int, str] = {}  # channel_id -> project_id
        self._source_msg_index: Dict[int, str] = {}  # source_message_id -> project_id
        self._locks: Dict[str, asyncio.Lock] = {}
        self._load_all()

    # ------------------------------------------------------------------
    # Persistence

    def _project_path(self, project_id: str) -> Path:
        return self.state_dir / f"{project_id}.json"

    def _load_all(self) -> None:
        for p in self.state_dir.glob("*.json"):
            try:
                raw = json.loads(p.read_text())
                proj = self._project_from_dict(raw)
                self._projects[proj.id] = proj
                if proj.project_channel_id is not None:
                    self._channel_index[proj.project_channel_id] = proj.id
                for binding in proj.channels.values():
                    self._channel_index[binding.channel_id] = proj.id
                self._source_msg_index[proj.source_message_id] = proj.id
            except Exception:
                # Corrupt file — skip rather than crash the whole bot.
                continue

    def _save(self, project: Project) -> None:
        project.updated_at = _now()
        path = self._project_path(project.id)
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=str(self.state_dir), delete=False, suffix=".tmp"
        )
        try:
            json.dump(self._project_to_dict(project), tmp, indent=2, default=str)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.replace(tmp.name, path)

    @staticmethod
    def _project_to_dict(project: Project) -> Dict[str, Any]:
        d = asdict(project)
        d["status"] = project.status.value
        return d

    @staticmethod
    def _project_from_dict(d: Dict[str, Any]) -> Project:
        d = dict(d)
        d["status"] = ProjectStatus(d["status"])
        d["channels"] = {
            k: ChannelBinding(**v) for k, v in (d.get("channels") or {}).items()
        }
        d["design_versions"] = [
            DesignVersion(**v) for v in d.get("design_versions") or []
        ]
        d["reviews"] = [DesignReview(**v) for v in d.get("reviews") or []]
        d["approvals"] = [Approval(**v) for v in d.get("approvals") or []]
        d["tasks"] = {k: AgentTask(**v) for k, v in (d.get("tasks") or {}).items()}
        return Project(**d)

    # ------------------------------------------------------------------
    # Locks

    def lock(self, project_id: str) -> asyncio.Lock:
        lock = self._locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[project_id] = lock
        return lock

    # ------------------------------------------------------------------
    # Reads

    def get(self, project_id: str) -> Project:
        proj = self._projects.get(project_id)
        if proj is None:
            raise KeyError(f"Unknown project_id: {project_id}")
        return proj

    def all_projects(self) -> List[Project]:
        return list(self._projects.values())

    def project_by_channel(self, channel_id: int) -> Optional[Project]:
        pid = self._channel_index.get(channel_id)
        if pid is None:
            return None
        return self._projects.get(pid)

    def project_by_source_message(self, message_id: int) -> Optional[Project]:
        pid = self._source_msg_index.get(message_id)
        return self._projects.get(pid) if pid else None

    def assert_channel_in_project(self, project_id: str, channel_id: int) -> None:
        proj = self.get(project_id)
        if not proj.channel_belongs_to_project(channel_id):
            raise CrossProjectError(
                f"channel {channel_id} does not belong to project {project_id}"
            )

    # ------------------------------------------------------------------
    # Writes (each persists immediately)

    def create_project(
        self,
        slug: str,
        title: str,
        source_message_id: int,
        owner_user_id: int,
        workspace_path: str,
    ) -> Project:
        # Idempotency by source_message_id.
        existing = self.project_by_source_message(source_message_id)
        if existing is not None:
            return existing
        project_id = _new_id("proj")
        proj = Project(
            id=project_id,
            slug=slug,
            title=title,
            source_message_id=source_message_id,
            owner_user_id=owner_user_id,
            status=ProjectStatus.INTAKE,
            workspace_path=workspace_path,
        )
        self._projects[project_id] = proj
        self._source_msg_index[source_message_id] = project_id
        self.append_audit(project_id, "project_created", {"slug": slug, "title": title})
        return proj

    def attach_project_channel(self, project_id: str, channel_id: int) -> None:
        proj = self.get(project_id)
        if proj.project_channel_id is not None and proj.project_channel_id != channel_id:
            raise ValueError(
                f"project {project_id} already has channel {proj.project_channel_id}"
            )
        proj.project_channel_id = channel_id
        self._channel_index[channel_id] = project_id
        binding = ChannelBinding(
            project_id=project_id,
            channel_id=channel_id,
            channel_type=CHANNEL_TYPE_PROJECT,
            allowed_roles=["athena", "prometheus", "argus"],
        )
        proj.channels[CHANNEL_TYPE_PROJECT] = binding
        self.append_audit(project_id, "project_channel_attached", {"channel_id": channel_id})

    def attach_task_channel(
        self,
        project_id: str,
        channel_id: int,
        channel_type: str,
        allowed_roles: List[str],
    ) -> ChannelBinding:
        proj = self.get(project_id)
        if channel_type in proj.channels:
            existing = proj.channels[channel_type]
            if existing.channel_id == channel_id:
                return existing
            raise ValueError(
                f"project {project_id} already has a {channel_type} channel"
            )
        binding = ChannelBinding(
            project_id=project_id,
            channel_id=channel_id,
            channel_type=channel_type,
            allowed_roles=allowed_roles,
        )
        proj.channels[channel_type] = binding
        self._channel_index[channel_id] = project_id
        self.append_audit(
            project_id,
            "task_channel_attached",
            {"channel_id": channel_id, "channel_type": channel_type},
        )
        return binding

    def set_status(self, project_id: str, status: ProjectStatus) -> None:
        proj = self.get(project_id)
        prev = proj.status
        proj.status = status
        self.append_audit(
            project_id, "status_changed", {"from": prev.value, "to": status.value}
        )

    def add_design_version(
        self, project_id: str, body: str, author_role: str
    ) -> DesignVersion:
        proj = self.get(project_id)
        version = (proj.design_versions[-1].version + 1) if proj.design_versions else 1
        dv = DesignVersion(version=version, body=body, author_role=author_role)
        proj.design_versions.append(dv)
        self.append_audit(
            project_id, "design_added", {"version": version, "author": author_role}
        )
        return dv

    def add_review(
        self,
        project_id: str,
        design_version: int,
        comments: str,
        decision: str,
    ) -> DesignReview:
        proj = self.get(project_id)
        review = DesignReview(
            project_id=project_id,
            design_version=design_version,
            review_run_id=_new_id("rev"),
            comments=comments,
            decision=decision,
        )
        proj.reviews.append(review)
        self.append_audit(
            project_id,
            "review_added",
            {"design_version": design_version, "decision": decision},
        )
        return review

    def record_approval(
        self,
        project_id: str,
        design_version: int,
        user_id: int,
        decision: str,
    ) -> Approval:
        proj = self.get(project_id)
        appr = Approval(
            project_id=project_id,
            design_version=design_version,
            user_id=user_id,
            decision=decision,
        )
        proj.approvals.append(appr)
        self.append_audit(
            project_id,
            "approval_recorded",
            {"user_id": user_id, "decision": decision, "design_version": design_version},
        )
        return appr

    def add_task(
        self,
        project_id: str,
        role: str,
        provider: str,
        channel_id: int,
        workspace_path: str,
        input_design_version: int,
        instructions: str,
    ) -> AgentTask:
        proj = self.get(project_id)
        # channel must belong to this project
        self.assert_channel_in_project(project_id, channel_id)
        task = AgentTask(
            project_id=project_id,
            task_id=_new_id("task"),
            role=role,
            provider=provider,
            channel_id=channel_id,
            workspace_path=workspace_path,
            input_design_version=input_design_version,
            instructions=instructions,
        )
        proj.tasks[task.task_id] = task
        self.append_audit(
            project_id,
            "task_created",
            {"task_id": task.task_id, "role": role, "channel_id": channel_id},
        )
        return task

    def update_task(
        self,
        project_id: str,
        task_id: str,
        *,
        status: Optional[str] = None,
        output_summary: Optional[str] = None,
        started_at: Optional[float] = None,
        finished_at: Optional[float] = None,
    ) -> AgentTask:
        proj = self.get(project_id)
        task = proj.tasks[task_id]
        if status is not None:
            task.status = status
        if output_summary is not None:
            task.output_summary = output_summary
        if started_at is not None:
            task.started_at = started_at
        if finished_at is not None:
            task.finished_at = finished_at
        self.append_audit(
            project_id,
            "task_updated",
            {"task_id": task_id, "status": task.status},
        )
        return task

    def append_audit(self, project_id: str, event: str, data: Dict[str, Any]) -> None:
        proj = self._projects.get(project_id)
        if proj is None:
            return
        proj.audit_log.append({"ts": _now(), "event": event, "data": data})

    def save(self, project_id: str) -> None:
        self._save(self._projects[project_id])

    # ------------------------------------------------------------------
    # Cross-project guard

    def ensure_no_cross_project_routing(
        self, target_project_id: str, claimed_channel_ids: Iterable[int]
    ) -> None:
        for cid in claimed_channel_ids:
            owner = self._channel_index.get(cid)
            if owner is not None and owner != target_project_id:
                self.append_audit(
                    target_project_id,
                    "cross_project_blocked",
                    {"channel_id": cid, "actual_owner": owner},
                )
                raise CrossProjectError(
                    f"channel {cid} belongs to {owner}, not {target_project_id}"
                )
