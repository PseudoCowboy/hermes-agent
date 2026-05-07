"""Domain models for mythos.

Plain dataclasses; SQLite is the persistence layer in store.py.
"""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class ProjectState(str, enum.Enum):
    INTAKE = "intake"
    CLARIFYING = "clarifying"
    DRAFTING = "drafting"
    REVIEWING = "reviewing"
    AWAITING_APPROVAL = "awaiting_approval"
    REVISING = "revising"
    DECOMPOSING = "decomposing"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"


class Discipline(str, enum.Enum):
    FRONTEND = "frontend"
    BACKEND = "backend"
    TEST = "test"


# Discipline -> agent role
DISCIPLINE_AGENT = {
    Discipline.FRONTEND: "apollo",
    Discipline.BACKEND: "atlas",
    Discipline.TEST: "hephaestus",
}


@dataclass
class Project:
    project_id: str
    owner_user_id: int
    seed_request: str
    project_channel_id: int = 0
    state: ProjectState = ProjectState.INTAKE
    created_at: float = field(default_factory=time.time)
    spec_version: int = 0
    review_iteration: int = 0
    discipline_channels: Dict[str, int] = field(default_factory=dict)  # discipline -> channel_id
    working_dir: str = ""

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]


@dataclass
class SpecVersion:
    project_id: str
    version: int
    content: str
    created_at: float = field(default_factory=time.time)


@dataclass
class Review:
    project_id: str
    spec_version: int
    iteration: int
    content: str
    created_at: float = field(default_factory=time.time)


@dataclass
class ApprovalEvent:
    project_id: str
    spec_version: int
    user_id: int
    approved_at: float = field(default_factory=time.time)
