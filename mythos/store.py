"""Persistent JSON-on-disk store for mythos projects, channels, tasks,
artifacts, runs, approvals, and audit events.

Recovery rule (NFR-001): on restart, reload all projects and active state
without losing artifact references.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from mythos.models import (
    AgentRun,
    Approval,
    Artifact,
    AuditEvent,
    Project,
    ProjectChannel,
    Task,
)


class JsonStore:
    """Single-process JSON store, one file per record type per project.

    Layout:
      <state_dir>/projects/<project_id>/project.json
      <state_dir>/projects/<project_id>/channels.json
      <state_dir>/projects/<project_id>/tasks.json
      <state_dir>/projects/<project_id>/artifacts.json
      <state_dir>/projects/<project_id>/runs.json
      <state_dir>/projects/<project_id>/approvals.json
      <state_dir>/projects/<project_id>/audit.jsonl
      <state_dir>/index.json   # source_message_id / channel_id -> project_id
    """

    def __init__(self, state_dir: str | Path):
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "projects").mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._index_path = self.root / "index.json"

    # ------------ helpers ----------------------------------------------------

    def _proj_dir(self, project_id: str) -> Path:
        d = self.root / "projects" / project_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default

    def _write_json(self, path: Path, data: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    # ------------ projects ---------------------------------------------------

    def save_project(self, project: Project) -> None:
        with self._lock:
            self._write_json(
                self._proj_dir(project.id) / "project.json", project.to_dict()
            )

    def get_project(self, project_id: str) -> Optional[Project]:
        with self._lock:
            data = self._read_json(self._proj_dir(project_id) / "project.json", None)
            return Project.from_dict(data) if data else None

    def list_projects(self) -> List[Project]:
        with self._lock:
            out: List[Project] = []
            base = self.root / "projects"
            if not base.exists():
                return out
            for child in sorted(base.iterdir()):
                if not child.is_dir():
                    continue
                data = self._read_json(child / "project.json", None)
                if data:
                    out.append(Project.from_dict(data))
            return out

    # ------------ channels ---------------------------------------------------

    def save_channel(self, channel: ProjectChannel) -> None:
        with self._lock:
            path = self._proj_dir(channel.project_id) / "channels.json"
            existing = self._read_json(path, [])
            # de-dupe by id
            existing = [c for c in existing if c.get("id") != channel.id]
            existing.append(channel.to_dict())
            self._write_json(path, existing)

    def list_channels(self, project_id: str) -> List[ProjectChannel]:
        with self._lock:
            raw = self._read_json(self._proj_dir(project_id) / "channels.json", [])
            return [ProjectChannel.from_dict(c) for c in raw]

    def find_channel(self, project_id: str, channel_role: str) -> Optional[ProjectChannel]:
        for c in self.list_channels(project_id):
            if c.channel_role.value == channel_role:
                return c
        return None

    # ------------ tasks ------------------------------------------------------

    def save_task(self, task: Task) -> None:
        with self._lock:
            path = self._proj_dir(task.project_id) / "tasks.json"
            existing = self._read_json(path, [])
            existing = [t for t in existing if t.get("id") != task.id]
            existing.append(task.to_dict())
            self._write_json(path, existing)

    def list_tasks(self, project_id: str) -> List[Task]:
        with self._lock:
            raw = self._read_json(self._proj_dir(project_id) / "tasks.json", [])
            return [Task.from_dict(t) for t in raw]

    # ------------ artifacts --------------------------------------------------

    def save_artifact(self, artifact: Artifact) -> None:
        with self._lock:
            path = self._proj_dir(artifact.project_id) / "artifacts.json"
            existing = self._read_json(path, [])
            existing = [a for a in existing if a.get("id") != artifact.id]
            existing.append(artifact.to_dict())
            self._write_json(path, existing)

    def list_artifacts(self, project_id: str) -> List[Artifact]:
        with self._lock:
            raw = self._read_json(self._proj_dir(project_id) / "artifacts.json", [])
            return [Artifact.from_dict(a) for a in raw]

    def get_artifact(self, project_id: str, artifact_id: str) -> Optional[Artifact]:
        for a in self.list_artifacts(project_id):
            if a.id == artifact_id:
                return a
        return None

    # ------------ runs -------------------------------------------------------

    def save_run(self, run: AgentRun) -> None:
        with self._lock:
            path = self._proj_dir(run.project_id) / "runs.json"
            existing = self._read_json(path, [])
            existing = [r for r in existing if r.get("id") != run.id]
            existing.append(run.to_dict())
            self._write_json(path, existing)

    def list_runs(self, project_id: str) -> List[AgentRun]:
        with self._lock:
            raw = self._read_json(self._proj_dir(project_id) / "runs.json", [])
            return [AgentRun.from_dict(r) for r in raw]

    # ------------ approvals --------------------------------------------------

    def save_approval(self, approval: Approval) -> None:
        with self._lock:
            path = self._proj_dir(approval.project_id) / "approvals.json"
            existing = self._read_json(path, [])
            existing = [a for a in existing if a.get("id") != approval.id]
            existing.append(approval.to_dict())
            self._write_json(path, existing)

    def list_approvals(self, project_id: str) -> List[Approval]:
        with self._lock:
            raw = self._read_json(self._proj_dir(project_id) / "approvals.json", [])
            return [Approval.from_dict(a) for a in raw]

    # ------------ audit ------------------------------------------------------

    def append_audit(self, event: AuditEvent) -> None:
        with self._lock:
            path = self._proj_dir(event.project_id) / "audit.jsonl"
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict()) + "\n")

    def iter_audit(self, project_id: str) -> Iterator[AuditEvent]:
        path = self._proj_dir(project_id) / "audit.jsonl"
        if not path.exists():
            return iter([])
        events: List[AuditEvent] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(AuditEvent.from_dict(json.loads(line)))
            except json.JSONDecodeError:
                continue
        return iter(events)

    # ------------ channel index ---------------------------------------------

    def find_project_by_channel(self, discord_channel_id: str) -> Optional[Project]:
        """Reverse-lookup: given a Discord channel ID, find its project."""
        with self._lock:
            for proj in self.list_projects():
                for ch in self.list_channels(proj.id):
                    if ch.discord_channel_id == discord_channel_id:
                        return proj
            return None

    def find_channel_role_for(
        self, project_id: str, discord_channel_id: str
    ) -> Optional[str]:
        for ch in self.list_channels(project_id):
            if ch.discord_channel_id == discord_channel_id:
                return ch.channel_role.value
        return None
