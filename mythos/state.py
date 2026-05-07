"""Mythos state types and project state machine.

Each project is a small finite state machine that controls the workflow
between the main, draft-plan, review, and specialist agents. The states
match the spec.md `Phase`s and `Acceptance Scenarios`. Transitions are
explicit and only valid moves are allowed — see :meth:`Project.transition_to`.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set


class ProjectState(str, Enum):
    """Project lifecycle states (FR-003, plan.md Phase 1)."""

    INTAKE = "intake"  # captured, channel not yet created
    PLANNING = "planning"  # draft plan agent working / clarifying
    REVIEW = "review"  # draft handed off to review agent
    AWAITING_APPROVAL = "awaiting_approval"  # review done, waiting for user
    DECOMPOSING = "decomposing"  # creating workstream channels
    IMPLEMENTING = "implementing"  # specialist agents working
    TESTING = "testing"  # test agent verifying
    COMPLETE = "complete"
    BLOCKED = "blocked"  # CLI failure, channel creation failure, etc.


# Allowed transitions (kept narrow on purpose — keeps the FSM auditable).
_ALLOWED_TRANSITIONS: Dict[ProjectState, Set[ProjectState]] = {
    ProjectState.INTAKE: {ProjectState.PLANNING, ProjectState.BLOCKED},
    ProjectState.PLANNING: {ProjectState.REVIEW, ProjectState.PLANNING, ProjectState.BLOCKED},
    ProjectState.REVIEW: {
        ProjectState.PLANNING,  # revision round
        ProjectState.AWAITING_APPROVAL,
        ProjectState.BLOCKED,
    },
    ProjectState.AWAITING_APPROVAL: {
        ProjectState.PLANNING,  # user requested changes
        ProjectState.DECOMPOSING,
        ProjectState.BLOCKED,
    },
    ProjectState.DECOMPOSING: {ProjectState.IMPLEMENTING, ProjectState.BLOCKED},
    ProjectState.IMPLEMENTING: {ProjectState.TESTING, ProjectState.BLOCKED, ProjectState.COMPLETE},
    ProjectState.TESTING: {ProjectState.COMPLETE, ProjectState.IMPLEMENTING, ProjectState.BLOCKED},
    ProjectState.BLOCKED: set(),  # terminal until manually reset
    ProjectState.COMPLETE: set(),
}


class WorkstreamKind(str, Enum):
    FRONTEND = "frontend"
    BACKEND = "backend"
    TEST = "test"


@dataclass
class DesignSpec:
    """A version of the project's design spec, produced by the draft plan agent."""

    version: int
    body: str
    created_at: float = field(default_factory=time.time)


@dataclass
class ReviewComments:
    """One review pass over a specific design spec version."""

    spec_version: int
    body: str
    blocking: bool = False
    created_at: float = field(default_factory=time.time)


@dataclass
class Workstream:
    kind: WorkstreamKind
    channel_id: Optional[int] = None  # Discord channel id once created
    channel_name: Optional[str] = None
    workdir: Optional[Path] = None
    status: str = "pending"  # pending | in_progress | done | blocked
    last_message: Optional[str] = None


@dataclass
class Project:
    """A single user-submitted software project.

    Each project is isolated: its own channel, workdir, agent context, and
    state. The orchestrator ensures messages and artifacts never cross
    projects (FR-022, SC-002).
    """

    id: str
    request: str
    requester_id: int  # Discord user ID
    state: ProjectState = ProjectState.INTAKE
    project_channel_id: Optional[int] = None
    project_channel_name: Optional[str] = None
    workdir: Optional[Path] = None

    specs: List[DesignSpec] = field(default_factory=list)
    reviews: List[ReviewComments] = field(default_factory=list)
    workstreams: Dict[WorkstreamKind, Workstream] = field(default_factory=dict)
    revision_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_error: Optional[str] = None

    # Lock protects in-place state mutation when the orchestrator and Discord
    # event loop touch the project concurrently.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    @staticmethod
    def new(request: str, requester_id: int) -> "Project":
        return Project(
            id=uuid.uuid4().hex[:12],
            request=request,
            requester_id=requester_id,
        )

    def transition_to(self, new_state: ProjectState) -> None:
        """Move the project to ``new_state``. Raises ``ValueError`` if the
        transition is not allowed by the FSM table.
        """
        with self._lock:
            allowed = _ALLOWED_TRANSITIONS[self.state]
            if new_state not in allowed and new_state is not self.state:
                raise ValueError(
                    f"illegal state transition: {self.state.value} -> {new_state.value} "
                    f"(allowed: {sorted(s.value for s in allowed)})"
                )
            self.state = new_state

    def latest_spec(self) -> Optional[DesignSpec]:
        return self.specs[-1] if self.specs else None

    def latest_review(self) -> Optional[ReviewComments]:
        return self.reviews[-1] if self.reviews else None

    def add_spec(self, body: str) -> DesignSpec:
        version = (self.latest_spec().version + 1) if self.specs else 1
        spec = DesignSpec(version=version, body=body)
        self.specs.append(spec)
        return spec

    def add_review(self, body: str, *, blocking: bool = False) -> ReviewComments:
        spec = self.latest_spec()
        if spec is None:
            raise RuntimeError("cannot review without a spec")
        review = ReviewComments(spec_version=spec.version, body=body, blocking=blocking)
        self.reviews.append(review)
        return review


class ProjectRegistry:
    """Thread-safe in-memory registry of all live projects.

    Acts as the single source of truth for "which channel belongs to which
    project" — used by the orchestrator to enforce per-channel agent
    confinement and project isolation.
    """

    def __init__(self) -> None:
        self._projects: Dict[str, Project] = {}
        self._channel_index: Dict[int, str] = {}  # channel_id -> project_id
        self._workstream_index: Dict[int, tuple] = {}  # channel_id -> (project_id, kind)
        self._lock = threading.RLock()

    def register(self, project: Project) -> None:
        with self._lock:
            self._projects[project.id] = project

    def get(self, project_id: str) -> Optional[Project]:
        with self._lock:
            return self._projects.get(project_id)

    def all(self) -> List[Project]:
        with self._lock:
            return list(self._projects.values())

    def bind_project_channel(self, project: Project, channel_id: int) -> None:
        with self._lock:
            project.project_channel_id = channel_id
            self._channel_index[channel_id] = project.id

    def bind_workstream_channel(
        self,
        project: Project,
        kind: WorkstreamKind,
        channel_id: int,
    ) -> None:
        with self._lock:
            ws = project.workstreams.get(kind)
            if ws is None:
                ws = Workstream(kind=kind)
                project.workstreams[kind] = ws
            ws.channel_id = channel_id
            self._workstream_index[channel_id] = (project.id, kind)

    def project_for_channel(self, channel_id: int) -> Optional[Project]:
        with self._lock:
            pid = self._channel_index.get(channel_id)
            if pid is None:
                ws_entry = self._workstream_index.get(channel_id)
                if ws_entry is None:
                    return None
                pid = ws_entry[0]
            return self._projects.get(pid)

    def workstream_kind_for_channel(self, channel_id: int) -> Optional[WorkstreamKind]:
        with self._lock:
            entry = self._workstream_index.get(channel_id)
            return entry[1] if entry else None
