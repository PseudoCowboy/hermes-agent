"""Tests for hermes_cli.runstate (P7a-1, write-only API).

Cover:
* Stream + project runstate writes produce expected JSON keyed by spec §12 schema.
* Atomic semantics — mid-write crash leaves prior state intact.
* Concurrent writes serialize via the workflow project lock.
* Validation rejects bad statuses / phases / turn_count.
* Read API stubs raise NotImplementedError (P7b).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from hermes_cli.runstate import (
    _runstate_path_project,
    _runstate_path_stream,
    list_stream_runstates,
    read_project_runstate,
    read_stream_runstate,
    write_project_runstate,
    write_stream_runstate,
)


@pytest.fixture()
def projects_root(tmp_path, monkeypatch):
    """Point the workflow projects root at a tmp dir."""
    root = tmp_path / "groups" / "shared_project" / "active"
    root.mkdir(parents=True)
    monkeypatch.setenv("WORKFLOW_PROJECTS_ROOT", str(root))
    monkeypatch.chdir(tmp_path)
    return root


# -----------------------------------------------------------------------------
# Path resolution
# -----------------------------------------------------------------------------


def test_project_runstate_path_under_scope(projects_root):
    p = _runstate_path_project("scope-1", "billing")
    # The scope is spliced before the active dir, so files for two scopes
    # with the same slug never collide.
    assert "scope-1" in str(p)
    assert p.name == "project_runstate.json"


def test_stream_runstate_path_under_workstream(projects_root):
    p = _runstate_path_stream("scope-1", "billing", "frontend")
    assert p.parent.name == "frontend"
    assert p.parent.parent.name == "workstreams"
    assert p.name == "runstate.json"


def test_stream_path_rejects_dotdot(projects_root):
    with pytest.raises(ValueError):
        _runstate_path_stream("scope-1", "billing", "..")


def test_stream_path_rejects_separator(projects_root):
    with pytest.raises(ValueError):
        _runstate_path_stream("scope-1", "billing", "frontend/api")


# -----------------------------------------------------------------------------
# Write semantics — project
# -----------------------------------------------------------------------------


def test_write_project_runstate_creates_file_with_skeleton_fields(projects_root):
    write_project_runstate(
        "scope-1",
        "billing",
        phase="approved",
        approved_head_sha="abc123",
        main_channel_id="999",
    )
    path = _runstate_path_project("scope-1", "billing")
    assert path.is_file()
    data = json.loads(path.read_text())
    assert data["scope_id"] == "scope-1"
    assert data["slug"] == "billing"
    assert data["phase"] == "approved"
    assert data["approved_head_sha"] == "abc123"
    assert data["main_channel_id"] == "999"
    # Skeleton defaults present.
    assert data["merged_streams"] == []
    assert data["active_merge_stream"] is None
    assert data["updated_at"]


def test_write_project_runstate_preserves_existing_fields(projects_root):
    write_project_runstate(
        "scope-1",
        "billing",
        phase="approved",
        approved_head_sha="abc",
        main_channel_id="111",
    )
    write_project_runstate(
        "scope-1",
        "billing",
        phase="streams-active",
    )
    data = json.loads(_runstate_path_project("scope-1", "billing").read_text())
    assert data["phase"] == "streams-active"
    # main_channel_id from prior write must survive partial update.
    assert data["main_channel_id"] == "111"
    assert data["approved_head_sha"] == "abc"


def test_write_project_runstate_rejects_bad_phase(projects_root):
    with pytest.raises(ValueError):
        write_project_runstate("scope-1", "billing", phase="banana")


def test_write_project_runstate_rejects_non_list_merged_streams(projects_root):
    with pytest.raises(ValueError):
        write_project_runstate("scope-1", "billing", merged_streams="frontend")


# -----------------------------------------------------------------------------
# Write semantics — stream
# -----------------------------------------------------------------------------


def test_write_stream_runstate_creates_file(projects_root):
    write_stream_runstate(
        "scope-1",
        "billing",
        "frontend",
        status="awaiting-first-turn",
        channel_id="123",
        approved_head_sha="abc",
    )
    path = _runstate_path_stream("scope-1", "billing", "frontend")
    assert path.is_file()
    data = json.loads(path.read_text())
    assert data["status"] == "awaiting-first-turn"
    assert data["channel_id"] == "123"
    assert data["scope_id"] == "scope-1"
    assert data["slug"] == "billing"
    assert data["stream"] == "frontend"
    assert data["turn_count"] == 0
    assert data["updated_at"]


def test_write_stream_runstate_preserves_existing(projects_root):
    write_stream_runstate(
        "scope-1", "billing", "frontend",
        status="awaiting-first-turn",
        channel_id="123",
        approved_head_sha="abc",
    )
    write_stream_runstate(
        "scope-1", "billing", "frontend",
        status="running",
        turn_count=2,
    )
    data = json.loads(
        _runstate_path_stream("scope-1", "billing", "frontend").read_text()
    )
    assert data["status"] == "running"
    assert data["turn_count"] == 2
    # Older fields must persist.
    assert data["channel_id"] == "123"
    assert data["approved_head_sha"] == "abc"


def test_write_stream_runstate_rejects_bad_status(projects_root):
    with pytest.raises(ValueError):
        write_stream_runstate(
            "scope-1", "billing", "frontend",
            status="not-a-status",
        )


def test_write_stream_runstate_rejects_bad_turn_count(projects_root):
    with pytest.raises(ValueError):
        write_stream_runstate(
            "scope-1", "billing", "frontend",
            turn_count=-1,
        )
    with pytest.raises(ValueError):
        write_stream_runstate(
            "scope-1", "billing", "frontend",
            turn_count="five",
        )


def test_write_stream_runstate_corrupt_prior_file_overwritten(projects_root):
    """A corrupt prior runstate must NOT block a new write.

    If we refuse to overwrite garbage, the project gets stuck — there is
    no admin tool in P7a-1 to clean it up.  Better to recover by
    flattening to a fresh skeleton + new fields.
    """
    path = _runstate_path_stream("scope-1", "billing", "frontend")
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json")
    write_stream_runstate(
        "scope-1", "billing", "frontend", status="running",
    )
    data = json.loads(path.read_text())
    assert data["status"] == "running"


# -----------------------------------------------------------------------------
# Atomicity: mid-write crash leaves prior state intact
# -----------------------------------------------------------------------------


def test_atomic_write_leaves_prior_state_on_failure(projects_root, monkeypatch):
    """If os.replace fails, the previous file content must remain.

    We simulate the failure by patching ``os.replace`` to raise after the
    first write has already landed the prior good state.  The atomic
    helper uses mkstemp + replace; failing replace leaves the original
    file untouched.
    """
    write_stream_runstate(
        "scope-1", "billing", "frontend",
        status="awaiting-first-turn", channel_id="123",
    )
    path = _runstate_path_stream("scope-1", "billing", "frontend")
    original = path.read_text()

    real_replace = os.replace

    def _failing_replace(src, dst, *args, **kwargs):
        # Only fail when targeting the runstate file itself; let the
        # workflow lock file replacements (none here, but safety net) pass.
        if str(dst) == str(path):
            raise OSError("simulated replace failure")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr("hermes_cli.runstate.os.replace", _failing_replace)

    with pytest.raises(OSError):
        write_stream_runstate(
            "scope-1", "billing", "frontend", status="running",
        )
    # The on-disk content must equal the original.
    assert path.read_text() == original


# -----------------------------------------------------------------------------
# Concurrency: serialized via the workflow project lock
# -----------------------------------------------------------------------------


def test_concurrent_writes_do_not_lose_updates(projects_root):
    """Two threads writing different fields concurrently should both
    survive — read-modify-write under the project lock means the second
    writer sees the first writer's changes.
    """
    write_stream_runstate(
        "scope-1", "billing", "frontend",
        status="awaiting-first-turn", channel_id="111",
    )

    barrier = threading.Barrier(2)

    def _writer_a():
        barrier.wait()
        write_stream_runstate(
            "scope-1", "billing", "frontend",
            status="running", turn_count=1,
        )

    def _writer_b():
        barrier.wait()
        write_stream_runstate(
            "scope-1", "billing", "frontend",
            approved_head_sha="abc-from-b",
        )

    t1 = threading.Thread(target=_writer_a)
    t2 = threading.Thread(target=_writer_b)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    data = json.loads(
        _runstate_path_stream("scope-1", "billing", "frontend").read_text()
    )
    # Both writers' updates must be present; ordering may vary but no
    # earlier field may be lost.  The original channel_id="111" must
    # still be there.
    assert data["channel_id"] == "111"
    assert data["approved_head_sha"] == "abc-from-b"
    # Last-write-wins on overlapping fields, but both touched disjoint
    # field sets — so status was set by writer_a and survives.
    assert data["status"] == "running"


# -----------------------------------------------------------------------------
# Read API stubs (P7b)
# -----------------------------------------------------------------------------


def test_read_project_runstate_not_implemented(projects_root):
    with pytest.raises(NotImplementedError):
        read_project_runstate("scope-1", "billing")


def test_read_stream_runstate_not_implemented(projects_root):
    with pytest.raises(NotImplementedError):
        read_stream_runstate("scope-1", "billing", "frontend")


def test_list_stream_runstates_not_implemented(projects_root):
    with pytest.raises(NotImplementedError):
        list_stream_runstates("scope-1", "billing")
