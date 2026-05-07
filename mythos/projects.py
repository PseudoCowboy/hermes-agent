"""Project metadata, workspace, and persistence layer.

A *project* in Mythos is the unit of isolation:

* Unique id of the form ``proj_<short-uuid>``.
* A Discord channel category id plus a dict of sub-channel ids.
* A workspace directory under ``MythosConfig.workspace_root``.
* A row in a small JSON-backed metadata store.
* A status enum that tracks the workflow phase.

The store is intentionally JSON, not SQLite, so it has zero install-time
dependencies (the spec calls for SQLite but allows "lightweight datastore"
- a JSON file is the smallest possible thing that still satisfies the
'persistence + recovery on bot restart' requirement).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class ProjectStatus(str, Enum):
    INTAKE = "intake"
    PLANNING_CLARIFY = "planning_clarify"
    PLANNING_DRAFTING = "planning_drafting"
    PLANNING_REVIEW = "planning_review"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    DECOMPOSED = "decomposed"
    IMPLEMENTING = "implementing"
    AWAITING_TESTS = "awaiting_tests"
    DONE = "done"
    PAUSED = "paused"
    ARCHIVED = "archived"


@dataclass
class Project:
    id: str
    name: str
    slug: str
    status: ProjectStatus = ProjectStatus.INTAKE
    requester_user_id: str = ""
    intake_text: str = ""
    channel_category_id: str = ""
    # role -> discord channel id (e.g. "planning" -> "...", "frontend" -> "...")
    sub_channel_ids: Dict[str, str] = field(default_factory=dict)
    approved_spec_hash: str = ""
    decomposition_manifest_path: str = ""
    workspace_path: str = ""
    planning_rounds: int = 0
    handoff_streak: int = 0
    completion_reports: Dict[str, str] = field(default_factory=dict)  # role -> message
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Project":
        data = dict(data)
        data["status"] = ProjectStatus(data.get("status", "intake"))
        return cls(**data)


def slugify(text: str, max_len: int = 32) -> str:
    """Crude slug for project / category names."""
    out: List[str] = []
    for ch in text.strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in (" ", "-", "_"):
            out.append("-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:max_len] or "project"


def make_project_id() -> str:
    return f"proj_{uuid.uuid4().hex[:8]}"


class ProjectStore:
    """Thread-safe JSON-backed project store."""

    def __init__(self, db_path: str, workspace_root: str):
        self._path = Path(db_path)
        self._workspace_root = Path(workspace_root)
        self._lock = threading.RLock()
        self._projects: Dict[str, Project] = {}
        self._channel_index: Dict[str, str] = {}  # channel_id -> project_id
        self._load()

    # --------------------------------------------------------------- io

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except json.JSONDecodeError:
            return
        for raw in data.get("projects", []):
            p = Project.from_dict(raw)
            self._projects[p.id] = p
            for channel_id in p.sub_channel_ids.values():
                if channel_id:
                    self._channel_index[channel_id] = p.id
            if p.channel_category_id:
                self._channel_index[p.channel_category_id] = p.id

    def _persist_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {"projects": [p.to_dict() for p in self._projects.values()]}
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self._path)

    # --------------------------------------------------------------- queries

    def list(self) -> List[Project]:
        with self._lock:
            return list(self._projects.values())

    def get(self, project_id: str) -> Optional[Project]:
        with self._lock:
            return self._projects.get(project_id)

    def get_by_channel(self, channel_id: str) -> Optional[Project]:
        with self._lock:
            pid = self._channel_index.get(channel_id)
            if not pid:
                return None
            return self._projects.get(pid)

    def active_count(self) -> int:
        with self._lock:
            return sum(
                1
                for p in self._projects.values()
                if p.status not in (ProjectStatus.ARCHIVED, ProjectStatus.DONE)
            )

    # --------------------------------------------------------------- mutations

    def create(self, *, name: str, requester_user_id: str, intake_text: str) -> Project:
        with self._lock:
            slug = slugify(name)
            project_id = make_project_id()
            workspace_path = self._workspace_root / project_id
            workspace_path.mkdir(parents=True, exist_ok=True)
            project = Project(
                id=project_id,
                name=name,
                slug=slug,
                requester_user_id=requester_user_id,
                intake_text=intake_text,
                workspace_path=str(workspace_path.resolve()),
            )
            self._projects[project_id] = project
            self._persist_locked()
            return project

    def update(self, project: Project) -> None:
        with self._lock:
            project.updated_at = time.time()
            self._projects[project.id] = project
            for channel_id in project.sub_channel_ids.values():
                if channel_id:
                    self._channel_index[channel_id] = project.id
            if project.channel_category_id:
                self._channel_index[project.channel_category_id] = project.id
            self._persist_locked()

    def archive(self, project_id: str) -> None:
        with self._lock:
            p = self._projects.get(project_id)
            if not p:
                return
            p.status = ProjectStatus.ARCHIVED
            p.updated_at = time.time()
            self._persist_locked()

    # --------------------------------------------------------------- helpers

    def workspace_for(self, project: Project) -> Path:
        return Path(project.workspace_path)
