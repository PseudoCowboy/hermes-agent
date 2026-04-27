"""E2E test for the plan-approval → stream-bootstrap pipe (P7a-1).

The orchestrator session_agent_worker has a post-turn hook that:

1. Reads-and-truncates ``control/approval-event.json`` (written by
   ``workflow_approve_plan``).
2. Loads ``workstreams/manifest.json``.
3. Calls ``bootstrap_streams_for_project(...)`` with manifest + sha.
4. On failure, posts an error to the main channel; success path is
   silent at this layer (the bootstrap posts its own summary).

We test the hook directly (``_maybe_run_stream_bootstrap``) rather than
running the full agent worker — that keeps the test focused on the
hook's contract and avoids needing a real AIAgent + persona prompt.

Coverage:
* Marker present + valid manifest → bootstrap invoked, marker consumed.
* No marker → no-op.
* Marker present + missing manifest → error posted, marker still consumed.
* Marker present + bootstrap returns error → error posted, marker
  consumed (no infinite retry).
* Marker present + missing guild_id → error posted.
* Corrupt marker → discarded, no-op.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

import pytest

from gateway.session_agent_worker import (
    _consume_approval_event_marker,
    _maybe_run_stream_bootstrap,
)


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


# -----------------------------------------------------------------------------
# Fakes — minimal, just what the hook touches
# -----------------------------------------------------------------------------


class FakeChannel:
    def __init__(self, channel_id, name, topic="", category=None):
        self.id = channel_id
        self.name = name
        self.topic = topic
        self.category = category
        self.deleted = False

    async def delete(self, reason=None):
        self.deleted = True


class FakeCategory:
    def __init__(self, category_id, name):
        self.id = category_id
        self.name = name
        self.channels: List[FakeChannel] = []
        self._next_channel_id = category_id + 1

    async def create_text_channel(self, name, topic=None, overwrites=None, reason=None):
        ch = FakeChannel(self._next_channel_id, name, topic or "", category=self)
        self._next_channel_id += 1
        self.channels.append(ch)
        return ch


class FakeGuild:
    def __init__(self, guild_id):
        self.id = guild_id
        self.categories: List[FakeCategory] = []

    def add_category(self, cat):
        self.categories.append(cat)


class FakeClient:
    def __init__(self, guild):
        self._guild = guild

    def get_guild(self, gid):
        return self._guild if gid == self._guild.id else None

    def _all_channels(self):
        out = {}
        for cat in self._guild.categories:
            out[cat.id] = cat
            for ch in cat.channels:
                out[ch.id] = ch
        return out

    def get_channel(self, cid):
        return self._all_channels().get(cid)

    async def fetch_channel(self, cid):
        ch = self._all_channels().get(cid)
        if ch is None:
            raise RuntimeError(f"no such channel {cid}")
        return ch


class FakeAdapter:
    def __init__(self, guild=None):
        self._client = FakeClient(guild) if guild is not None else None
        self.bot_user_id = 12345
        self.sent: List[tuple] = []
        self._no_auto_thread_channels: set = set()

    async def send(self, chat_id, content, **kwargs):
        self.sent.append((str(chat_id), content))


class FakeRunner:
    """Just enough of GatewayRunner for the hook to do its job."""

    def __init__(self, guild_id: Optional[str] = "12345"):
        from gateway.session import Platform
        from gateway.session_router import SessionRouter

        self._session_router = SessionRouter()
        # Minimal config object that mimics PlatformConfig structure.
        discord_cfg = SimpleNamespace(orchestration_guild_id=guild_id)
        self.config = SimpleNamespace(
            platforms={Platform.DISCORD: discord_cfg},
            group_sessions_per_user=True,
            thread_sessions_per_user=False,
        )
        self.session_store = None
        self._project_merge_locks: dict = {}
        self._project_merge_locks_guard = asyncio.Lock()

    def _session_key_for_source(self, source):
        from gateway.session import build_session_key

        return build_session_key(source)

    async def merge_lock_for(self, scope_id, slug):
        key = (scope_id or "", slug)
        async with self._project_merge_locks_guard:
            lock = self._project_merge_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._project_merge_locks[key] = lock
            return lock


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture()
def project_on_disk(tmp_path, monkeypatch):
    """Real git repo + an active project directory with manifest written.

    Project is laid out at the SCOPED path (cat.id=9000) since the
    runtime path resolver splices scope into the projects root.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)
    (repo / "README.md").write_text("seed\n")
    _git(["add", "README.md"], cwd=repo)
    _git(["commit", "-m", "seed"], cwd=repo)

    projects_root = repo / "groups" / "shared_project" / "active"
    projects_root.mkdir(parents=True)
    monkeypatch.setenv("WORKFLOW_PROJECTS_ROOT", str(projects_root))
    monkeypatch.chdir(repo)

    # Materialise the project at the scoped path matching cat.id=9000
    # used by the FakeCategory in guild_with_category.  project_path()
    # splices scope_id between bundle dir and "active", so the actual
    # project root is groups/shared_project/9000/active/billing.
    from tools.workflow_tools import project_path

    project = project_path("billing", "9000")
    (project / "control").mkdir(parents=True)
    (project / "workstreams").mkdir(parents=True)
    (project / "coordination").mkdir(parents=True)
    (project / "plans").mkdir(parents=True)

    # Workstream manifest with 1 stream.
    manifest = {"frontend": {"agentRole": "frontend"}}
    (project / "workstreams" / "manifest.json").write_text(json.dumps(manifest))

    return repo, project


@pytest.fixture()
def guild_with_category():
    guild = FakeGuild(guild_id=12345)
    cat = FakeCategory(9000, "billing")
    guild.add_category(cat)
    main = FakeChannel(9001, "billing-main", category=cat)
    cat.channels.append(main)
    return guild, cat, main


def _make_orchestrator_session(scope_id, slug, main_channel_id):
    from gateway.session_router import LongLivedSession

    s = LongLivedSession(f"{scope_id}:{slug}")
    s.scope_id = scope_id
    s.slug = slug
    s.main_channel_id = str(main_channel_id)
    s.persona = "orchestrator"
    return s


def _write_marker(project_root, sha="abc123"):
    marker = project_root / "control" / "approval-event.json"
    marker.write_text(json.dumps({
        "event": "approved",
        "approved_at": "2026-04-27T12:00:00Z",
        "approved_head_sha": sha,
    }))
    return marker


async def _cancel_session_workers(runner):
    for sess in list(runner._session_router._sessions.values()):
        if sess._in_flight is not None:
            sess._in_flight.cancel()
            try:
                await sess._in_flight
            except (asyncio.CancelledError, BaseException):
                pass


# -----------------------------------------------------------------------------
# Marker present + valid manifest → bootstrap invoked
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_runs_bootstrap_when_marker_present(
    project_on_disk, guild_with_category,
):
    repo, project = project_on_disk
    guild, cat, main = guild_with_category
    adapter = FakeAdapter(guild=guild)
    runner = FakeRunner()
    session = _make_orchestrator_session(
        scope_id=str(cat.id), slug="billing", main_channel_id=main.id,
    )

    marker = _write_marker(project)

    await _maybe_run_stream_bootstrap(session, runner, adapter)

    # Marker consumed.
    assert not marker.exists()
    # Stream channel created under the category.
    stream_chans = [c for c in cat.channels if c is not main]
    assert len(stream_chans) == 1
    assert stream_chans[0].name == "billing-frontend"
    # Session registered.
    assert len(runner._session_router) == 1
    # Bootstrap posted summary to main channel.
    assert any(c[0] == str(main.id) for c in adapter.sent)

    await _cancel_session_workers(runner)


# -----------------------------------------------------------------------------
# No marker → no-op
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_is_noop_without_marker(project_on_disk, guild_with_category):
    _, project = project_on_disk
    guild, cat, main = guild_with_category
    adapter = FakeAdapter(guild=guild)
    runner = FakeRunner()
    session = _make_orchestrator_session(
        scope_id=str(cat.id), slug="billing", main_channel_id=main.id,
    )

    # No marker written.
    await _maybe_run_stream_bootstrap(session, runner, adapter)

    assert adapter.sent == []
    assert len(runner._session_router) == 0
    # Only the pre-existing main channel.
    assert [c.name for c in cat.channels] == ["billing-main"]


# -----------------------------------------------------------------------------
# Marker + missing manifest → error posted, marker consumed
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_posts_error_when_manifest_missing(
    project_on_disk, guild_with_category,
):
    _, project = project_on_disk
    guild, cat, main = guild_with_category
    adapter = FakeAdapter(guild=guild)
    runner = FakeRunner()
    session = _make_orchestrator_session(
        scope_id=str(cat.id), slug="billing", main_channel_id=main.id,
    )

    marker = _write_marker(project)
    # Remove manifest.
    (project / "workstreams" / "manifest.json").unlink()

    await _maybe_run_stream_bootstrap(session, runner, adapter)

    # Marker still consumed (no infinite retry).
    assert not marker.exists()
    # Error posted to main channel.
    main_posts = [c for c in adapter.sent if c[0] == str(main.id)]
    assert len(main_posts) == 1
    assert "manifest" in main_posts[0][1].lower()
    # No streams created.
    assert [c.name for c in cat.channels] == ["billing-main"]


# -----------------------------------------------------------------------------
# Marker + bootstrap fails → error posted, marker consumed
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_posts_error_when_bootstrap_returns_error(
    project_on_disk, guild_with_category,
):
    _, project = project_on_disk
    guild, cat, main = guild_with_category
    adapter = FakeAdapter(guild=guild)
    runner = FakeRunner()
    session = _make_orchestrator_session(
        scope_id=str(cat.id), slug="billing", main_channel_id=main.id,
    )

    # Write a manifest with a bad agentRole — bootstrap rejects it
    # before any side effects, returning an error.
    (project / "workstreams" / "manifest.json").write_text(
        json.dumps({"frontend": {"agentRole": "fullstack"}})
    )
    marker = _write_marker(project)

    await _maybe_run_stream_bootstrap(session, runner, adapter)

    # Marker consumed.
    assert not marker.exists()
    # Error message posted.
    main_posts = [c for c in adapter.sent if c[0] == str(main.id)]
    assert len(main_posts) == 1
    assert "bootstrap failed" in main_posts[0][1].lower()


# -----------------------------------------------------------------------------
# Missing guild_id → error posted, marker consumed
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_posts_error_when_guild_id_missing(
    project_on_disk, guild_with_category,
):
    _, project = project_on_disk
    guild, cat, main = guild_with_category
    adapter = FakeAdapter(guild=guild)
    runner = FakeRunner(guild_id=None)  # not configured
    session = _make_orchestrator_session(
        scope_id=str(cat.id), slug="billing", main_channel_id=main.id,
    )

    marker = _write_marker(project)

    await _maybe_run_stream_bootstrap(session, runner, adapter)

    assert not marker.exists()
    main_posts = [c for c in adapter.sent if c[0] == str(main.id)]
    assert len(main_posts) == 1
    assert "guild_id" in main_posts[0][1].lower() or "guild" in main_posts[0][1].lower()


# -----------------------------------------------------------------------------
# Corrupt marker → discarded, no-op
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_discards_corrupt_marker(
    project_on_disk, guild_with_category,
):
    _, project = project_on_disk
    guild, cat, main = guild_with_category
    adapter = FakeAdapter(guild=guild)
    runner = FakeRunner()
    session = _make_orchestrator_session(
        scope_id=str(cat.id), slug="billing", main_channel_id=main.id,
    )

    # Write a corrupt marker (not valid JSON).
    marker = project / "control" / "approval-event.json"
    marker.write_text("{not valid json")

    await _maybe_run_stream_bootstrap(session, runner, adapter)

    # Corrupt marker is unlinked.
    assert not marker.exists()
    # No streams created, nothing posted.
    assert [c.name for c in cat.channels] == ["billing-main"]
    assert adapter.sent == []


# -----------------------------------------------------------------------------
# _consume_approval_event_marker direct unit tests
# -----------------------------------------------------------------------------


def test_consume_marker_returns_payload_and_unlinks(project_on_disk):
    _, project = project_on_disk
    cat_id = "9000"
    session = _make_orchestrator_session(cat_id, "billing", "9001")

    marker = _write_marker(project, sha="deadbeef")
    payload = _consume_approval_event_marker(session)

    assert payload is not None
    assert payload["event"] == "approved"
    assert payload["approved_head_sha"] == "deadbeef"
    assert not marker.exists()


def test_consume_marker_returns_none_when_absent(project_on_disk):
    _, project = project_on_disk
    cat_id = "9000"
    session = _make_orchestrator_session(cat_id, "billing", "9001")

    payload = _consume_approval_event_marker(session)
    assert payload is None


def test_consume_marker_handles_empty_file(project_on_disk):
    _, project = project_on_disk
    cat_id = "9000"
    session = _make_orchestrator_session(cat_id, "billing", "9001")

    marker = project / "control" / "approval-event.json"
    marker.write_text("")
    payload = _consume_approval_event_marker(session)

    assert payload is None
    # Empty marker is unlinked too.
    assert not marker.exists()


# -----------------------------------------------------------------------------
# Payload validation: malformed marker is rejected (codex critical fix)
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_rejects_marker_without_event_field(
    project_on_disk, guild_with_category,
):
    _, project = project_on_disk
    guild, cat, main = guild_with_category
    adapter = FakeAdapter(guild=guild)
    runner = FakeRunner()
    session = _make_orchestrator_session(
        scope_id=str(cat.id), slug="billing", main_channel_id=main.id,
    )

    # Marker without ``event=approved`` — must NOT trigger bootstrap.
    marker = project / "control" / "approval-event.json"
    marker.write_text(json.dumps({
        "approved_head_sha": "abc123",
        # event field omitted entirely
    }))

    await _maybe_run_stream_bootstrap(session, runner, adapter)

    # Marker consumed (so bad payload doesn't get re-tried in a loop).
    assert not marker.exists()
    # Error posted, no streams created.
    assert [c.name for c in cat.channels] == ["billing-main"]
    assert len(runner._session_router) == 0
    main_posts = [c for c in adapter.sent if c[0] == str(main.id)]
    assert len(main_posts) == 1
    assert "malformed" in main_posts[0][1].lower() or "approved" in main_posts[0][1].lower()


@pytest.mark.asyncio
async def test_hook_rejects_marker_without_approved_head_sha(
    project_on_disk, guild_with_category,
):
    _, project = project_on_disk
    guild, cat, main = guild_with_category
    adapter = FakeAdapter(guild=guild)
    runner = FakeRunner()
    session = _make_orchestrator_session(
        scope_id=str(cat.id), slug="billing", main_channel_id=main.id,
    )

    # Marker with event=approved but no approved_head_sha.
    marker = project / "control" / "approval-event.json"
    marker.write_text(json.dumps({
        "event": "approved",
        "approved_head_sha": None,
    }))

    await _maybe_run_stream_bootstrap(session, runner, adapter)

    assert not marker.exists()
    assert [c.name for c in cat.channels] == ["billing-main"]
    assert len(runner._session_router) == 0
    main_posts = [c for c in adapter.sent if c[0] == str(main.id)]
    assert len(main_posts) == 1

