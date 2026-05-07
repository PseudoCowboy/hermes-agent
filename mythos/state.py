"""Project state model + JSON-backed store.

We keep state simple (no SQL): one JSON file per project, plus an index.
This is enough for the MVP and survives process restarts (NFR1).
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .roles import ChannelKind, ProjectStatus, Role


@dataclass
class ChannelRef:
    kind: ChannelKind
    discord_channel_id: int
    name: str

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind.value, "discord_channel_id": self.discord_channel_id, "name": self.name}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChannelRef":
        return cls(kind=ChannelKind(data["kind"]),
                   discord_channel_id=int(data["discord_channel_id"]),
                   name=data["name"])


@dataclass
class DesignSpec:
    version: int
    content: str
    created_at: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewComment:
    spec_version: int
    content: str
    created_at: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ApprovalEvent:
    spec_version: int
    approver_user_id: int
    approved_at: float
    message_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TaskRecord:
    task_id: str
    role: Role
    channel_kind: ChannelKind
    status: str  # queued | running | completed | failed | needs_input
    summary: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["role"] = self.role.value
        d["channel_kind"] = self.channel_kind.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskRecord":
        return cls(
            task_id=data["task_id"],
            role=Role(data["role"]),
            channel_kind=ChannelKind(data["channel_kind"]),
            status=data["status"],
            summary=data.get("summary", ""),
            created_at=float(data.get("created_at") or time.time()),
            finished_at=data.get("finished_at"),
        )


@dataclass
class Project:
    project_id: str
    short_id: str
    slug: str
    status: ProjectStatus
    created_by_discord_user_id: int
    main_channel_id: int
    original_request: str
    workspace_path: str
    category_id: Optional[int] = None
    channels: Dict[str, ChannelRef] = field(default_factory=dict)
    design_specs: List[DesignSpec] = field(default_factory=list)
    review_comments: List[ReviewComment] = field(default_factory=list)
    approvals: List[ApprovalEvent] = field(default_factory=list)
    tasks: List[TaskRecord] = field(default_factory=list)
    audit: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # ---- Helpers --------------------------------------------------------

    def latest_spec(self) -> Optional[DesignSpec]:
        return self.design_specs[-1] if self.design_specs else None

    def latest_approval(self) -> Optional[ApprovalEvent]:
        return self.approvals[-1] if self.approvals else None

    def is_approved(self) -> bool:
        spec = self.latest_spec()
        approval = self.latest_approval()
        return spec is not None and approval is not None and approval.spec_version == spec.version

    def channel_for(self, kind: ChannelKind) -> Optional[ChannelRef]:
        return self.channels.get(kind.value)

    def channel_kind_for(self, channel_id: int) -> Optional[ChannelKind]:
        for ref in self.channels.values():
            if ref.discord_channel_id == channel_id:
                return ref.kind
        return None

    def append_audit(self, event_type: str, payload: Optional[Dict[str, Any]] = None,
                     actor: str = "system") -> None:
        self.audit.append({
            "ts": time.time(),
            "actor": actor,
            "event_type": event_type,
            "payload": payload or {},
        })
        self.updated_at = time.time()

    # ---- Serialization --------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "short_id": self.short_id,
            "slug": self.slug,
            "status": self.status.value,
            "created_by_discord_user_id": self.created_by_discord_user_id,
            "main_channel_id": self.main_channel_id,
            "original_request": self.original_request,
            "workspace_path": self.workspace_path,
            "category_id": self.category_id,
            "channels": {k: v.to_dict() for k, v in self.channels.items()},
            "design_specs": [s.to_dict() for s in self.design_specs],
            "review_comments": [r.to_dict() for r in self.review_comments],
            "approvals": [a.to_dict() for a in self.approvals],
            "tasks": [t.to_dict() for t in self.tasks],
            "audit": self.audit,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Project":
        proj = cls(
            project_id=data["project_id"],
            short_id=data["short_id"],
            slug=data["slug"],
            status=ProjectStatus(data["status"]),
            created_by_discord_user_id=int(data["created_by_discord_user_id"]),
            main_channel_id=int(data["main_channel_id"]),
            original_request=data["original_request"],
            workspace_path=data["workspace_path"],
            category_id=data.get("category_id"),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )
        proj.channels = {k: ChannelRef.from_dict(v) for k, v in (data.get("channels") or {}).items()}
        proj.design_specs = [DesignSpec(**s) for s in (data.get("design_specs") or [])]
        proj.review_comments = [ReviewComment(**r) for r in (data.get("review_comments") or [])]
        proj.approvals = [ApprovalEvent(**a) for a in (data.get("approvals") or [])]
        proj.tasks = [TaskRecord.from_dict(t) for t in (data.get("tasks") or [])]
        proj.audit = list(data.get("audit") or [])
        return proj


# ---------------------------------------------------------------------------


def new_project_id() -> tuple[str, str]:
    """Return (project_id, short_id)."""
    short = secrets.token_hex(3)
    return f"proj_{short}_{int(time.time())}", short


def slugify(text: str, max_len: int = 30) -> str:
    out = []
    last_dash = False
    for ch in text.lower().strip():
        if ch.isalnum():
            out.append(ch)
            last_dash = False
        elif not last_dash:
            out.append("-")
            last_dash = True
    s = "".join(out).strip("-")
    return (s[:max_len].rstrip("-") or "project")


class ProjectStore:
    """JSON-backed project store.

    State layout::

        <state_path>           (index file: { "projects": {short_id: filename} })
        <state_path>.dir/
          <project_id>.json    (one file per project)
    """

    def __init__(self, state_path: Path):
        self.state_path = Path(state_path)
        self.dir_path = self.state_path.parent / (self.state_path.name + ".d")
        self._lock = threading.RLock()
        self._cache: Dict[str, Project] = {}
        self._loaded = False

    # ---- IO --------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self.dir_path.mkdir(parents=True, exist_ok=True)
            for f in sorted(self.dir_path.glob("*.json")):
                try:
                    data = json.loads(f.read_text())
                    proj = Project.from_dict(data)
                    self._cache[proj.project_id] = proj
                except Exception:
                    continue
            self._loaded = True

    def _persist(self, project: Project) -> None:
        self.dir_path.mkdir(parents=True, exist_ok=True)
        path = self.dir_path / f"{project.project_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(project.to_dict(), indent=2, sort_keys=True))
        tmp.replace(path)

    # ---- Public API ------------------------------------------------------

    def create(self, project: Project) -> None:
        self._ensure_loaded()
        with self._lock:
            self._cache[project.project_id] = project
            self._persist(project)

    def save(self, project: Project) -> None:
        with self._lock:
            project.updated_at = time.time()
            self._cache[project.project_id] = project
            self._persist(project)

    def get(self, project_id: str) -> Optional[Project]:
        self._ensure_loaded()
        return self._cache.get(project_id)

    def find_by_channel(self, channel_id: int) -> Optional[Project]:
        self._ensure_loaded()
        for p in self._cache.values():
            if p.channel_kind_for(channel_id) is not None:
                return p
            if p.category_id == channel_id:
                return p
        return None

    def list(self) -> List[Project]:
        self._ensure_loaded()
        return list(self._cache.values())
