"""In-memory + JSONL-backed project store.

Each project is an isolated record. The store also maintains channel→project
and channel→workstream lookup tables so the Discord bridge can route messages
to the correct project/agent without leaking across projects.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from mythos.models import (
    AgentRole,
    AgentRun,
    ApprovalDecision,
    Artifact,
    Project,
    ProjectState,
    ReviewComment,
    Spec,
    Workstream,
    WorkstreamType,
)
from mythos.state_machine import transition

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slugify(name: str, *, max_len: int = 50) -> str:
    s = name.lower().strip()
    s = re.sub(r"\s+", "-", s)
    s = _SLUG_RE.sub("", s)
    s = s.strip("-")
    if not s:
        s = "project"
    return s[:max_len]


def _serialize(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj):
        return {k: _serialize(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    return obj


class ProjectStore:
    """Tracks all projects with strict isolation.

    Channel id → project id and channel id → workstream id maps enforce that an
    incoming Discord message can only be associated with one project.
    """

    def __init__(self, *, persistence_path: str | Path | None = None) -> None:
        self._projects: dict[str, Project] = {}
        self._channel_to_project: dict[int, str] = {}
        self._channel_to_workstream: dict[int, str] = {}
        self._slugs_in_use: set[str] = set()
        self._lock = asyncio.Lock()
        self._persistence_path = Path(persistence_path) if persistence_path else None
        if self._persistence_path:
            self._persistence_path.parent.mkdir(parents=True, exist_ok=True)

    # --- mutation helpers ---------------------------------------------------

    async def create_project(
        self,
        *,
        display_name: str,
        requester: str,
        request_text: str,
        source_message_id: str,
    ) -> Project:
        async with self._lock:
            slug_base = _slugify(display_name)
            slug = slug_base
            i = 2
            while slug in self._slugs_in_use:
                slug = f"{slug_base}-{i}"
                i += 1
            self._slugs_in_use.add(slug)
            project = Project(
                display_name=display_name,
                requester=requester,
                request_text=request_text,
                source_message_id=source_message_id,
                project_channel_name=slug,
            )
            project.state_history.append((time.time(), ProjectState.INTAKE))
            self._projects[project.id] = project
            self._persist_event("project_created", {"project_id": project.id, "slug": slug})
            return project

    async def attach_project_channel(self, project_id: str, channel_id: int) -> None:
        async with self._lock:
            project = self._projects[project_id]
            project.project_channel_id = channel_id
            self._channel_to_project[channel_id] = project_id
            self._persist_event(
                "project_channel_attached",
                {"project_id": project_id, "channel_id": channel_id},
            )

    async def attach_workstream_channel(
        self, project_id: str, workstream_id: str, channel_id: int
    ) -> None:
        async with self._lock:
            project = self._projects[project_id]
            ws = project.workstreams[workstream_id]
            ws.channel_id = channel_id
            self._channel_to_workstream[channel_id] = workstream_id
            # Workstream channels also map back to their project for routing.
            self._channel_to_project[channel_id] = project_id
            self._persist_event(
                "workstream_channel_attached",
                {
                    "project_id": project_id,
                    "workstream_id": workstream_id,
                    "channel_id": channel_id,
                },
            )

    async def add_workstream(
        self, project_id: str, ws_type: WorkstreamType, role: AgentRole, scope: str, channel_name: str = ""
    ) -> Workstream:
        async with self._lock:
            project = self._projects[project_id]
            ws = Workstream(project_id=project_id, type=ws_type, role=role, scope=scope, channel_name=channel_name)
            project.workstreams[ws.id] = ws
            return ws

    async def add_spec(self, project_id: str, content: str) -> Spec:
        async with self._lock:
            project = self._projects[project_id]
            version = (project.latest_spec().version + 1) if project.latest_spec() else 1
            spec = Spec(project_id=project_id, version=version, content=content)
            project.specs.append(spec)
            return spec

    async def add_review(
        self,
        project_id: str,
        spec_version: int,
        text: str,
        recommendation: str,
        severity: str = "info",
    ) -> ReviewComment:
        async with self._lock:
            project = self._projects[project_id]
            comment = ReviewComment(
                spec_version=spec_version,
                reviewer=AgentRole.ARGUS,
                severity=severity,
                text=text,
                recommendation=recommendation,
            )
            project.review_comments.append(comment)
            spec = next((s for s in project.specs if s.version == spec_version), None)
            if spec is not None:
                spec.review_status = "acceptable" if recommendation == "accept" else "needs_changes"
            return comment

    async def add_approval(
        self,
        project_id: str,
        spec_version: int,
        approver: str,
        decision: str,
        requested_changes: str = "",
    ) -> ApprovalDecision:
        async with self._lock:
            project = self._projects[project_id]
            decision_obj = ApprovalDecision(
                spec_version=spec_version,
                approver=approver,
                decision=decision,
                requested_changes=requested_changes,
            )
            project.approvals.append(decision_obj)
            spec = next((s for s in project.specs if s.version == spec_version), None)
            if spec is not None:
                if decision == "approved":
                    spec.approval_status = "approved"
                elif decision == "rejected":
                    spec.approval_status = "rejected"
                else:
                    spec.approval_status = "revised"
            return decision_obj

    async def add_run(self, run: AgentRun) -> AgentRun:
        async with self._lock:
            project = self._projects[run.project_id]
            project.runs.append(run)
            return run

    async def add_artifact(self, artifact: Artifact) -> Artifact:
        async with self._lock:
            project = self._projects[artifact.project_id]
            project.artifacts.append(artifact)
            return artifact

    async def transition(self, project_id: str, to_state: ProjectState) -> None:
        async with self._lock:
            project = self._projects[project_id]
            transition(project, to_state)
            self._persist_event(
                "project_state_changed",
                {"project_id": project_id, "state": to_state.value},
            )

    # --- read helpers -------------------------------------------------------

    def get(self, project_id: str) -> Project:
        return self._projects[project_id]

    def list_active(self) -> list[Project]:
        return [
            p for p in self._projects.values()
            if p.state not in (ProjectState.ARCHIVED, ProjectState.COMPLETED)
        ]

    def all(self) -> list[Project]:
        return list(self._projects.values())

    def project_for_channel(self, channel_id: int) -> Project | None:
        pid = self._channel_to_project.get(channel_id)
        if pid is None:
            return None
        return self._projects.get(pid)

    def workstream_for_channel(self, channel_id: int) -> tuple[Project, Workstream] | None:
        ws_id = self._channel_to_workstream.get(channel_id)
        if ws_id is None:
            return None
        project = self.project_for_channel(channel_id)
        if project is None or ws_id not in project.workstreams:
            return None
        return project, project.workstreams[ws_id]

    # --- persistence --------------------------------------------------------

    def _persist_event(self, event: str, payload: dict[str, Any]) -> None:
        if not self._persistence_path:
            return
        record = {"ts": time.time(), "event": event, **payload}
        try:
            with self._persistence_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            # Persistence is best-effort; don't kill orchestration on disk error.
            pass

    def snapshot(self) -> list[dict[str, Any]]:
        return [_serialize(p) for p in self._projects.values()]
