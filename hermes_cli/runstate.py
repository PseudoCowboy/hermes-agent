"""Runstate writer for project + per-stream JSON files (P7a-1, write-only).

Two JSON files describe the live execution state of a project:

- ``project_runstate.json`` at the project root summarises the orchestrator's
  view of the whole project (phase, merged streams, approved head sha).
- ``workstreams/<stream>/runstate.json`` per stream summarises one
  implementer worker's view (status, turn count, awaiting reaction state).

Both schemas are documented in spec §12.

P7a-1 ships a *write-only* API (read + rehydration is P7b).  The writers
do read-modify-write semantics so callers can patch a subset of fields
without losing existing values, but they never expose those reads to
callers — every public write returns ``None``.

Concurrency model
-----------------

Writes happen from two places:

1. ``tools.workflow_tools.workflow_approve_plan`` (orchestrator turn) —
   already holds ``tools.workflow_tools._project_lock(slug, scope_id)``
   so its calls into ``write_project_runstate`` are already serialized.
2. ``gateway.implementer_worker`` per-stream worker — each stream has at
   most one worker by construction, so per-stream writes don't contend.

For safety we still wrap the read-modify-write in
``tools.workflow_tools._project_lock``: a future caller (e.g. merge_queue
in P7b) that doesn't hold the lock won't accidentally race a writer.
We import the private helper rather than duplicating it because the
intent is shared semantics — re-implementing risks subtle drift in lock
file paths.

Atomicity
---------

Each write goes through ``_atomic_write_text`` (mkstemp → fsync → rename).
A crash mid-write leaves the prior version intact.  This is the usual
POSIX rename trick; on Windows ``os.replace`` is also atomic.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


# We deliberately reach into ``tools.workflow_tools`` for the project
# lock + project_path helpers.  Both have stable semantics and are the
# canonical implementations — duplicating would risk silent drift in
# lock-file paths between writers.
from tools.workflow_tools import _project_lock, project_path


# Allowed values per spec §12.  Validated on write so a typo doesn't
# silently produce a runstate that the (P7b) reader can't classify.
_VALID_STREAM_STATUSES = frozenset({
    "awaiting-first-turn",
    "running",
    "paused",
    "review-pending",
    "review-accepted",
    "merged",
    "merge-conflict",
    "changes-requested",
})

_VALID_PROJECT_PHASES = frozenset({
    "draft",
    "approved",
    "streams-active",
    "merging",
    "integration-testing",
    "done",
    "paused",
})


# -----------------------------------------------------------------------------
# Path helpers
# -----------------------------------------------------------------------------


def _runstate_path_project(scope_id: Optional[str], slug: str) -> Path:
    """Path to ``project_runstate.json`` for one project."""
    return project_path(slug, scope_id) / "project_runstate.json"


def _runstate_path_stream(
    scope_id: Optional[str], slug: str, stream: str
) -> Path:
    """Path to ``workstreams/<stream>/runstate.json`` for one stream."""
    if not stream or os.sep in stream or "/" in stream or stream in {".", ".."}:
        raise ValueError(f"invalid stream name: {stream!r}")
    return project_path(slug, scope_id) / "workstreams" / stream / "runstate.json"


# -----------------------------------------------------------------------------
# Atomic write (local helper — the workflow_tools._atomic_write_text is
# private and reaching for it across modules adds no value here).
# -----------------------------------------------------------------------------


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace *path* with *text* (mkstemp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_text(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _read_existing(path: Path) -> Dict[str, Any]:
    """Best-effort read; missing/corrupt → empty dict.

    A corrupt prior file should NOT prevent a new write — the writer's
    job is to make the runstate observable; if the previous state was
    garbage, overwriting with a valid snapshot is the right move.
    """
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


# -----------------------------------------------------------------------------
# Stream runstate
# -----------------------------------------------------------------------------


# Default skeleton applied for first-write of a brand-new stream runstate.
# Kept aligned with spec §12 Appendix A so a fresh file is always
# parseable by P7b's reader, even if the caller forgot to set fields.
def _stream_skeleton(scope_id: Optional[str], slug: str, stream: str) -> Dict[str, Any]:
    return {
        "scope_id": scope_id or "",
        "slug": slug,
        "stream": stream,
        "status": "awaiting-first-turn",
        "channel_id": None,
        "prompt_message_id": None,
        "awaiting_user_id": None,
        "deadline_ts": None,
        "turn_count": 0,
        "approved_head_sha": None,
        "reason": None,
        "updated_at": _now_iso(),
    }


def _project_skeleton(scope_id: Optional[str], slug: str) -> Dict[str, Any]:
    return {
        "scope_id": scope_id or "",
        "slug": slug,
        "main_channel_id": None,
        "phase": "draft",
        "active_merge_stream": None,
        "merged_streams": [],
        "integration_test_started_ts": None,
        "approved_head_sha": None,
        "reason": None,
        "updated_at": _now_iso(),
    }


def write_stream_runstate(
    scope_id: Optional[str],
    slug: str,
    stream: str,
    **fields_to_update: Any,
) -> None:
    """Read-modify-write the stream runstate JSON.

    *fields_to_update* are merged on top of the existing file (or a fresh
    skeleton if absent).  ``updated_at`` is always refreshed.

    Raises ``ValueError`` if a known field is given an invalid value
    (status not in the spec set, turn_count not an int, etc.) — better
    to surface the bug at write time than let a P7b reader silently
    skip a malformed runstate.
    """
    if not slug:
        raise ValueError("slug is required")
    if "status" in fields_to_update:
        status = fields_to_update["status"]
        if status not in _VALID_STREAM_STATUSES:
            raise ValueError(
                f"invalid stream status {status!r}; expected one of "
                f"{sorted(_VALID_STREAM_STATUSES)}"
            )
    if "turn_count" in fields_to_update:
        turn_count = fields_to_update["turn_count"]
        if not isinstance(turn_count, int) or turn_count < 0:
            raise ValueError(
                f"invalid turn_count {turn_count!r}; expected non-negative int"
            )

    path = _runstate_path_stream(scope_id, slug, stream)
    with _project_lock(slug, scope_id):
        current = _read_existing(path) or _stream_skeleton(scope_id, slug, stream)
        # Defensive: ensure required identity fields are present even if
        # an earlier writer omitted them.  These are derived from inputs,
        # never user-supplied, so always overwrite.
        current["scope_id"] = scope_id or ""
        current["slug"] = slug
        current["stream"] = stream
        current.update(fields_to_update)
        current["updated_at"] = _now_iso()
        _atomic_write_text(path, _json_text(current))


def write_project_runstate(
    scope_id: Optional[str],
    slug: str,
    **fields_to_update: Any,
) -> None:
    """Read-modify-write the project runstate JSON.

    *fields_to_update* are merged on top of the existing file (or a
    fresh skeleton if absent).  ``updated_at`` is always refreshed.
    Validates ``phase`` if present.
    """
    if not slug:
        raise ValueError("slug is required")
    if "phase" in fields_to_update:
        phase = fields_to_update["phase"]
        if phase not in _VALID_PROJECT_PHASES:
            raise ValueError(
                f"invalid project phase {phase!r}; expected one of "
                f"{sorted(_VALID_PROJECT_PHASES)}"
            )
    if "merged_streams" in fields_to_update:
        ms = fields_to_update["merged_streams"]
        if not isinstance(ms, list) or not all(isinstance(s, str) for s in ms):
            raise ValueError(
                f"merged_streams must be a list of strings, got {ms!r}"
            )

    path = _runstate_path_project(scope_id, slug)
    with _project_lock(slug, scope_id):
        current = _read_existing(path) or _project_skeleton(scope_id, slug)
        current["scope_id"] = scope_id or ""
        current["slug"] = slug
        current.update(fields_to_update)
        current["updated_at"] = _now_iso()
        _atomic_write_text(path, _json_text(current))


# -----------------------------------------------------------------------------
# Read API stubs (P7b)
# -----------------------------------------------------------------------------


def read_project_runstate(scope_id: Optional[str], slug: str) -> Dict[str, Any]:
    """Read project runstate.  P7b — not implemented in P7a-1."""
    raise NotImplementedError("read_project_runstate lands in P7b")


def read_stream_runstate(
    scope_id: Optional[str], slug: str, stream: str
) -> Dict[str, Any]:
    """Read stream runstate.  P7b — not implemented in P7a-1."""
    raise NotImplementedError("read_stream_runstate lands in P7b")


def list_stream_runstates(scope_id: Optional[str], slug: str):
    """List all stream runstates for a project.  P7b — not implemented."""
    raise NotImplementedError("list_stream_runstates lands in P7b")
