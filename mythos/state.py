"""Project state model + JSON persistence.

Each project gets one ProjectRecord, persisted as a JSON file under
$HERMES_HOME/mythos/projects/<project_id>.json. The orchestrator never
holds canonical state in memory — it loads, mutates, and saves on every
transition so a restart can pick up exactly where things left off.

Idempotency: every transition that would create a new Discord channel
or kick off an agent run also records an idempotency key. The same
Discord event redelivered yields the same key and is skipped.
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
from typing import Dict, List, Optional


class ProjectPhase(str, Enum):
    INTAKE = "intake"
    CLARIFYING = "clarifying"
    DRAFTING = "drafting"
    REVIEWING = "reviewing"
    AWAITING_APPROVAL = "awaiting_approval"
    DECOMPOSING = "decomposing"
    IMPLEMENTING = "implementing"
    VALIDATING = "validating"
    COMPLETE = "complete"
    ERROR = "error"


class WorkstreamKind(str, Enum):
    FRONTEND = "frontend"
    BACKEND = "backend"
    TEST = "test"
    REVIEW = "review"


class WorkstreamStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class ReviewRound:
    round_number: int
    design_version: int
    review_comments: str = ""
    requested_changes: List[str] = field(default_factory=list)
    accepted: bool = False
    created_at: float = field(default_factory=time.time)


@dataclass
class WorkstreamRecord:
    kind: WorkstreamKind
    channel_id: Optional[str] = None
    channel_name: Optional[str] = None
    workspace_path: Optional[str] = None
    status: WorkstreamStatus = WorkstreamStatus.PENDING
    handoff_message: str = ""
    last_output: str = ""
    defects: List[str] = field(default_factory=list)
    run_ids: List[str] = field(default_factory=list)


@dataclass
class ProjectRecord:
    project_id: str
    source_message_id: str
    owner_user_id: str
    request_text: str
    main_channel_id: str
    project_channel_id: Optional[str] = None
    project_channel_name: Optional[str] = None
    workspace_path: Optional[str] = None
    phase: ProjectPhase = ProjectPhase.INTAKE
    clarifying_questions: List[str] = field(default_factory=list)
    clarifying_answers: List[str] = field(default_factory=list)
    design_versions: List[str] = field(default_factory=list)
    approved_design_version: Optional[int] = None
    review_rounds: List[ReviewRound] = field(default_factory=list)
    workstreams: Dict[str, WorkstreamRecord] = field(default_factory=dict)
    idempotency_keys: Dict[str, str] = field(default_factory=dict)
    error_message: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def latest_design(self) -> Optional[str]:
        return self.design_versions[-1] if self.design_versions else None

    def to_json(self) -> Dict:
        d = asdict(self)
        d["phase"] = self.phase.value
        d["workstreams"] = {
            k: {**asdict(v), "kind": v.kind.value, "status": v.status.value}
            for k, v in self.workstreams.items()
        }
        return d

    @classmethod
    def from_json(cls, data: Dict) -> "ProjectRecord":
        rounds = [ReviewRound(**r) for r in data.get("review_rounds", [])]
        ws_raw = data.get("workstreams", {})
        ws: Dict[str, WorkstreamRecord] = {}
        for k, v in ws_raw.items():
            ws[k] = WorkstreamRecord(
                kind=WorkstreamKind(v["kind"]),
                channel_id=v.get("channel_id"),
                channel_name=v.get("channel_name"),
                workspace_path=v.get("workspace_path"),
                status=WorkstreamStatus(v.get("status", "pending")),
                handoff_message=v.get("handoff_message", ""),
                last_output=v.get("last_output", ""),
                defects=list(v.get("defects", [])),
                run_ids=list(v.get("run_ids", [])),
            )
        return cls(
            project_id=data["project_id"],
            source_message_id=data["source_message_id"],
            owner_user_id=data["owner_user_id"],
            request_text=data["request_text"],
            main_channel_id=data["main_channel_id"],
            project_channel_id=data.get("project_channel_id"),
            project_channel_name=data.get("project_channel_name"),
            workspace_path=data.get("workspace_path"),
            phase=ProjectPhase(data.get("phase", "intake")),
            clarifying_questions=list(data.get("clarifying_questions", [])),
            clarifying_answers=list(data.get("clarifying_answers", [])),
            design_versions=list(data.get("design_versions", [])),
            approved_design_version=data.get("approved_design_version"),
            review_rounds=rounds,
            workstreams=ws,
            idempotency_keys=dict(data.get("idempotency_keys", {})),
            error_message=data.get("error_message"),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
        )


class ProjectStore:
    """File-backed project state store with per-project locking.

    The store is the single source of truth. Every mutation goes through
    ``with store.edit(project_id) as record:`` which loads, yields the
    record, and atomically writes it back on exit.
    """

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.projects_dir = self.base_dir / "projects"
        self.idempotency_dir = self.base_dir / "idempotency"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.idempotency_dir.mkdir(parents=True, exist_ok=True)
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ── locking ──────────────────────────────────────────────────────────
    def _lock_for(self, project_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(project_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[project_id] = lock
            return lock

    # ── path helpers ─────────────────────────────────────────────────────
    def _path(self, project_id: str) -> Path:
        return self.projects_dir / f"{project_id}.json"

    def _idem_path(self, key: str) -> Path:
        # Hash-shaped key: store flat — fine for small scale.
        safe = key.replace("/", "_").replace(":", "_")
        return self.idempotency_dir / f"{safe}.json"

    # ── CRUD ─────────────────────────────────────────────────────────────
    def create(self, record: ProjectRecord) -> None:
        path = self._path(record.project_id)
        if path.exists():
            raise FileExistsError(f"project already exists: {record.project_id}")
        self._atomic_write(path, record.to_json())

    def get(self, project_id: str) -> Optional[ProjectRecord]:
        path = self._path(project_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return ProjectRecord.from_json(json.load(fh))

    def list_ids(self) -> List[str]:
        return sorted(p.stem for p in self.projects_dir.glob("*.json"))

    def all(self) -> List[ProjectRecord]:
        out = []
        for pid in self.list_ids():
            rec = self.get(pid)
            if rec:
                out.append(rec)
        return out

    def save(self, record: ProjectRecord) -> None:
        record.updated_at = time.time()
        self._atomic_write(self._path(record.project_id), record.to_json())

    class _EditCtx:
        def __init__(self, store: "ProjectStore", project_id: str):
            self.store = store
            self.project_id = project_id
            self.record: Optional[ProjectRecord] = None
            self._lock = store._lock_for(project_id)

        def __enter__(self) -> ProjectRecord:
            self._lock.acquire()
            try:
                self.record = self.store.get(self.project_id)
                if self.record is None:
                    self._lock.release()
                    raise KeyError(self.project_id)
                return self.record
            except Exception:
                self._lock.release()
                raise

        def __exit__(self, exc_type, exc, tb):
            try:
                if exc is None and self.record is not None:
                    self.store.save(self.record)
            finally:
                self._lock.release()

    def edit(self, project_id: str) -> "ProjectStore._EditCtx":
        return ProjectStore._EditCtx(self, project_id)

    # ── idempotency ──────────────────────────────────────────────────────
    def claim_idempotency(self, key: str, payload: Optional[Dict] = None) -> bool:
        """Returns True if this key was newly claimed, False if already claimed."""
        path = self._idem_path(key)
        if path.exists():
            return False
        self._atomic_write(path, {"key": key, "claimed_at": time.time(), "payload": payload or {}})
        return True

    def lookup_idempotency(self, key: str) -> Optional[Dict]:
        path = self._idem_path(key)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _atomic_write(path: Path, data: Dict) -> None:
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


def new_project_id() -> str:
    return uuid.uuid4().hex[:12]
