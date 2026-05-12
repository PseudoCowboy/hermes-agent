"""SQLite-backed persistence for mythos projects.

Single-process, single-connection, thread-safe enough for our workload
(we hold a lock around writes). Sufficient for the v1 spec.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, List, Optional

from .models import (
    ApprovalEvent,
    Discipline,
    Project,
    ProjectState,
    Review,
    SpecVersion,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL,
    seed_request TEXT NOT NULL,
    project_channel_id INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    spec_version INTEGER NOT NULL DEFAULT 0,
    review_iteration INTEGER NOT NULL DEFAULT 0,
    discipline_channels TEXT NOT NULL DEFAULT '{}',
    completed_disciplines TEXT NOT NULL DEFAULT '[]',
    working_dir TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS spec_versions (
    project_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (project_id, version)
);

CREATE TABLE IF NOT EXISTS reviews (
    project_id TEXT NOT NULL,
    spec_version INTEGER NOT NULL,
    iteration INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (project_id, spec_version, iteration)
);

CREATE TABLE IF NOT EXISTS approvals (
    project_id TEXT NOT NULL,
    spec_version INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    approved_at REAL NOT NULL,
    PRIMARY KEY (project_id, spec_version)
);

CREATE INDEX IF NOT EXISTS idx_proj_channel ON projects(project_channel_id);
"""


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._ensure_project_columns()
            self._conn.commit()

    def _ensure_project_columns(self) -> None:
        """Apply tiny additive migrations for existing Mythos sqlite files."""
        cur = self._conn.execute("PRAGMA table_info(projects)")
        cols = {row[1] for row in cur.fetchall()}
        if "completed_disciplines" not in cols:
            self._conn.execute(
                "ALTER TABLE projects ADD COLUMN "
                "completed_disciplines TEXT NOT NULL DEFAULT '[]'"
            )

    @contextmanager
    def _cursor(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

    # ---- projects ----

    def insert_project(self, p: Project) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO projects (project_id, owner_user_id, seed_request,
                       project_channel_id, state, created_at, spec_version,
                       review_iteration, discipline_channels,
                       completed_disciplines, working_dir)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    p.project_id, p.owner_user_id, p.seed_request,
                    p.project_channel_id, p.state.value, p.created_at,
                    p.spec_version, p.review_iteration,
                    json.dumps(p.discipline_channels),
                    json.dumps(p.completed_disciplines), p.working_dir,
                ),
            )

    def update_project(self, p: Project) -> None:
        with self._cursor() as cur:
            cur.execute(
                """UPDATE projects SET project_channel_id=?, state=?, spec_version=?,
                       review_iteration=?, discipline_channels=?,
                       completed_disciplines=?, working_dir=?
                   WHERE project_id=?""",
                (
                    p.project_channel_id, p.state.value, p.spec_version,
                    p.review_iteration, json.dumps(p.discipline_channels),
                    json.dumps(p.completed_disciplines), p.working_dir,
                    p.project_id,
                ),
            )

    def get_project(self, project_id: str) -> Optional[Project]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM projects WHERE project_id=?", (project_id,))
            row = cur.fetchone()
        return _row_to_project(row) if row else None

    def get_project_by_channel(self, channel_id: int) -> Optional[Project]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM projects WHERE project_channel_id=?", (channel_id,))
            row = cur.fetchone()
        return _row_to_project(row) if row else None

    def list_projects(self) -> List[Project]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM projects ORDER BY created_at")
            rows = cur.fetchall()
        return [_row_to_project(r) for r in rows]

    # ---- spec versions ----

    def insert_spec(self, s: SpecVersion) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO spec_versions (project_id, version, content, created_at)
                   VALUES (?, ?, ?, ?)""",
                (s.project_id, s.version, s.content, s.created_at),
            )

    def latest_spec(self, project_id: str) -> Optional[SpecVersion]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM spec_versions WHERE project_id=? ORDER BY version DESC LIMIT 1",
                (project_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return SpecVersion(
            project_id=row["project_id"], version=row["version"],
            content=row["content"], created_at=row["created_at"],
        )

    # ---- reviews ----

    def insert_review(self, r: Review) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO reviews (project_id, spec_version, iteration, content, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (r.project_id, r.spec_version, r.iteration, r.content, r.created_at),
            )

    def latest_review(self, project_id: str) -> Optional[Review]:
        with self._cursor() as cur:
            cur.execute(
                """SELECT * FROM reviews WHERE project_id=?
                   ORDER BY spec_version DESC, iteration DESC LIMIT 1""",
                (project_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return Review(
            project_id=row["project_id"], spec_version=row["spec_version"],
            iteration=row["iteration"], content=row["content"], created_at=row["created_at"],
        )

    # ---- approvals ----

    def record_approval(self, a: ApprovalEvent) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO approvals (project_id, spec_version, user_id, approved_at)
                   VALUES (?, ?, ?, ?)""",
                (a.project_id, a.spec_version, a.user_id, a.approved_at),
            )

    def get_approval(self, project_id: str) -> Optional[ApprovalEvent]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM approvals WHERE project_id=? ORDER BY approved_at DESC LIMIT 1",
                (project_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return ApprovalEvent(
            project_id=row["project_id"], spec_version=row["spec_version"],
            user_id=row["user_id"], approved_at=row["approved_at"],
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        project_id=row["project_id"],
        owner_user_id=row["owner_user_id"],
        seed_request=row["seed_request"],
        project_channel_id=row["project_channel_id"],
        state=ProjectState(row["state"]),
        created_at=row["created_at"],
        spec_version=row["spec_version"],
        review_iteration=row["review_iteration"],
        discipline_channels=json.loads(row["discipline_channels"] or "{}"),
        completed_disciplines=json.loads(row["completed_disciplines"] or "[]"),
        working_dir=row["working_dir"],
    )
