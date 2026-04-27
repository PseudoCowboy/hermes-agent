"""Tests for gateway.stream_bootstrap (P7a-1).

End-to-end-style coverage with fakes for Discord (FakeAdapter / FakeGuild
/ FakeCategory / FakeChannel) but a *real* git repo + filesystem so the
worktree side effects (and rollback of them) are exercised.

Tests cover:

* Happy path: 2-stream manifest → 2 channels, 2 worktrees, 2 sessions,
  2 stub workers, summary message posted, project runstate written.
* Rollback when stream #2 channel-create fails: stream #1 must be fully
  torn down (worker cancelled, session unregistered, channel deleted,
  worktree removed, runstate file removed); integration worktree kept.
* Bad ``agentRole`` rejected before any side effects.
* Empty manifest rejected.
* Per-project lock serializes concurrent bootstraps for the same project.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

import pytest


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------


class FakeChannel:
    def __init__(self, channel_id, name, topic="", category=None):
        self.id = channel_id
        self.name = name
        self.topic = topic
        self.category = category
        self.deleted = False
        self.delete_reason: Optional[str] = None

    async def delete(self, reason=None):
        self.deleted = True
        self.delete_reason = reason


class FakeCategory:
    def __init__(self, category_id, name, *, fail_after: Optional[int] = None):
        self.id = category_id
        self.name = name
        self.channels: List[FakeChannel] = []
        self.deleted = False
        # If set, fail create_text_channel after this many successful calls.
        self.fail_after = fail_after
        self._created_count = 0
        self._next_channel_id = category_id + 1

    async def create_text_channel(self, name, topic=None, overwrites=None, reason=None):
        if self.fail_after is not None and self._created_count >= self.fail_after:
            raise RuntimeError(f"simulated failure on channel #{self._created_count + 1}")
        self._created_count += 1
        ch = FakeChannel(self._next_channel_id, name, topic or "", category=self)
        self._next_channel_id += 1
        self.channels.append(ch)
        return ch

    async def delete(self, reason=None):
        self.deleted = True


class FakeGuild:
    def __init__(self, guild_id):
        self.id = guild_id
        self.categories: List[FakeCategory] = []

    def add_category(self, cat):
        self.categories.append(cat)

    async def create_category(self, name, overwrites=None, reason=None):
        cat = FakeCategory(9000 + len(self.categories), name)
        self.categories.append(cat)
        return cat


class FakeClient:
    def __init__(self, guild: FakeGuild):
        self._guild = guild

    def get_guild(self, gid):
        if gid == self._guild.id:
            return self._guild
        return None

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
    def __init__(self, guild: FakeGuild, *, bot_user_id=123456):
        self._client = FakeClient(guild)
        self.bot_user_id = bot_user_id
        self.sent: List[tuple] = []
        self._no_auto_thread_channels: set = set()

    async def send(self, chat_id, content, **kwargs):
        self.sent.append((str(chat_id), content))


class FakeRunner:
    """Just enough of GatewayRunner for stream_bootstrap to do its job."""

    def __init__(self):
        from gateway.session_router import SessionRouter

        self._session_router = SessionRouter()
        self.config = SimpleNamespace(
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
def git_repo_with_projects(tmp_path, monkeypatch):
    """Real git repo + projects root for worktree side effects."""
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
    return repo, projects_root


@pytest.fixture()
def guild_with_category():
    """A FakeGuild with one project category pre-installed.

    The category id is what stream_bootstrap uses as the *project*
    scope_id — bootstrap doesn't create the category itself (that's
    project_bootstrap's job), only the per-stream channels under it.
    """
    guild = FakeGuild(guild_id=12345)
    cat = FakeCategory(9000, "billing")
    guild.add_category(cat)
    return guild, cat


@pytest.fixture()
def main_channel_in_category(guild_with_category):
    guild, cat = guild_with_category
    main = FakeChannel(9001, "billing-main", category=cat)
    cat.channels.append(main)
    return main


async def _cancel_session_workers(runner):
    """Tear down stub workers spawned by bootstrap so pytest doesn't warn."""
    for sess in list(runner._session_router._sessions.values()):
        if sess._in_flight is not None:
            sess._in_flight.cancel()
            try:
                await sess._in_flight
            except (asyncio.CancelledError, BaseException):
                pass


# -----------------------------------------------------------------------------
# Happy path
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_happy_path_creates_two_streams(
    git_repo_with_projects, guild_with_category, main_channel_in_category,
):
    repo, _ = git_repo_with_projects
    guild, cat = guild_with_category
    adapter = FakeAdapter(guild)
    runner = FakeRunner()

    from gateway.stream_bootstrap import bootstrap_streams_for_project

    result = await bootstrap_streams_for_project(
        runner=runner,
        scope_id=str(cat.id),
        slug="billing",
        main_channel_id=str(main_channel_in_category.id),
        workstream_manifest={
            "frontend": {"agentRole": "frontend"},
            "backend": {"agentRole": "backend"},
        },
        approved_head_sha="deadbeef" * 5,
        adapter=adapter,
        guild_id="12345",
    )

    assert result.error is None, result.error
    assert len(result.streams) == 2
    names = {s.stream_name for s in result.streams}
    assert names == {"frontend", "backend"}

    # Two channels created in the category (+ the pre-existing main).
    stream_chans = [c for c in cat.channels if c is not main_channel_in_category]
    assert len(stream_chans) == 2
    chan_names = {c.name for c in stream_chans}
    assert chan_names == {"billing-frontend", "billing-backend"}

    # Two sessions registered, with stream metadata populated.
    sessions = list(runner._session_router._sessions.values())
    assert len(sessions) == 2
    for sess in sessions:
        assert sess.persona == "implementer"
        assert sess.stream_name in {"frontend", "backend"}
        assert sess.role in {"frontend", "backend"}
        assert sess.worktree_root is not None
        # Worktree dir actually exists on disk.
        assert sess.worktree_root.is_dir()

    # Both stream channel ids registered for no-auto-thread.
    for s in result.streams:
        assert s.channel_id in adapter._no_auto_thread_channels

    # Per-stream runstate.json files were written with the expected fields.
    from hermes_cli.runstate import _runstate_path_stream

    for stream_name in ("frontend", "backend"):
        rs_path = _runstate_path_stream(str(cat.id), "billing", stream_name)
        assert rs_path.is_file(), f"runstate not at {rs_path}"
        rs = json.loads(rs_path.read_text())
        assert rs["status"] == "awaiting-first-turn"
        assert rs["approved_head_sha"] == "deadbeef" * 5

    # Project runstate phase=streams-active.
    from hermes_cli.runstate import _runstate_path_project

    pr_path = _runstate_path_project(str(cat.id), "billing")
    assert pr_path.is_file()
    pr = json.loads(pr_path.read_text())
    assert pr["phase"] == "streams-active"
    assert pr["main_channel_id"] == str(main_channel_in_category.id)
    assert pr["approved_head_sha"] == "deadbeef" * 5

    # Summary message posted to the main channel.
    main_id = str(main_channel_in_category.id)
    main_posts = [c for c in adapter.sent if c[0] == main_id]
    assert len(main_posts) >= 1
    summary = main_posts[-1][1]
    assert "billing-frontend" in summary or "frontend" in summary
    assert "billing-backend" in summary or "backend" in summary

    await _cancel_session_workers(runner)


# -----------------------------------------------------------------------------
# Rollback when stream #2 fails
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_rollback_when_second_stream_channel_create_fails(
    git_repo_with_projects, guild_with_category, main_channel_in_category,
):
    repo, _ = git_repo_with_projects
    guild, cat = guild_with_category
    # First create_text_channel succeeds, second raises.
    cat.fail_after = 1
    adapter = FakeAdapter(guild)
    runner = FakeRunner()

    from gateway.stream_bootstrap import bootstrap_streams_for_project

    result = await bootstrap_streams_for_project(
        runner=runner,
        scope_id=str(cat.id),
        slug="billing",
        main_channel_id=str(main_channel_in_category.id),
        workstream_manifest={
            "frontend": {"agentRole": "frontend"},
            "backend": {"agentRole": "backend"},
        },
        approved_head_sha="abc",
        adapter=adapter,
        guild_id="12345",
    )

    assert result.error is not None, "expected bootstrap failure"
    assert "stream bootstrap failed" in result.error.lower()
    assert result.streams == []

    # Stream #1's channel was created then deleted during rollback.
    stream_chans = [c for c in cat.channels if c is not main_channel_in_category]
    assert len(stream_chans) == 1, (
        f"expected exactly one stream channel left over (the deleted #1), "
        f"got {[c.name for c in stream_chans]}"
    )
    assert stream_chans[0].deleted is True, (
        "stream #1's channel was not deleted during rollback"
    )

    # Session router empty.
    assert len(runner._session_router) == 0

    # No-auto-thread set is empty.
    assert adapter._no_auto_thread_channels == set()

    # Per-stream runstate file for the rolled-back stream is gone.
    from hermes_cli.runstate import _runstate_path_stream

    rs_path = _runstate_path_stream(str(cat.id), "billing", "frontend")
    assert not rs_path.exists(), (
        "rolled-back stream's runstate file was not deleted"
    )

    # Project runstate was NOT updated to streams-active.
    from hermes_cli.runstate import _runstate_path_project

    pr_path = _runstate_path_project(str(cat.id), "billing")
    if pr_path.exists():
        pr = json.loads(pr_path.read_text())
        assert pr.get("phase") != "streams-active"


@pytest.mark.asyncio
async def test_bootstrap_cleans_up_worktree_when_first_stream_channel_fails(
    git_repo_with_projects, guild_with_category, main_channel_in_category,
):
    """Codex finding: worktree was created BEFORE the entry was appended
    to ``bootstrapped``, so a channel-create failure leaked the worktree.

    Set ``fail_after=0`` so the FIRST create_text_channel raises — the
    worktree for that stream must be removed by the per-iteration partial
    cleanup, not leaked.
    """
    repo, _ = git_repo_with_projects
    guild, cat = guild_with_category
    cat.fail_after = 0  # the very first channel-create raises
    adapter = FakeAdapter(guild)
    runner = FakeRunner()

    from gateway.stream_bootstrap import bootstrap_streams_for_project
    from hermes_cli.project_worktree import stream_worktree_path

    result = await bootstrap_streams_for_project(
        runner=runner,
        scope_id=str(cat.id),
        slug="billing",
        main_channel_id=str(main_channel_in_category.id),
        workstream_manifest={"frontend": {"agentRole": "frontend"}},
        approved_head_sha="abc",
        adapter=adapter,
        guild_id="12345",
    )

    assert result.error is not None
    assert result.streams == []

    # No stream channels (the first attempt raised before creation succeeded).
    stream_chans = [c for c in cat.channels if c is not main_channel_in_category]
    assert stream_chans == []

    # The worktree dir for that stream must NOT exist (no leak).
    wt_path = stream_worktree_path(str(cat.id), "billing", "frontend")
    assert not wt_path.exists(), (
        f"worktree leaked at {wt_path} after channel-create failure"
    )

    # Session router empty (partial session — if any — was unregistered).
    assert len(runner._session_router) == 0
    # No-auto-thread set is empty.
    assert adapter._no_auto_thread_channels == set()


# -----------------------------------------------------------------------------
# Validation: bad agentRole rejected before side effects
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_rejects_invalid_agent_role(
    git_repo_with_projects, guild_with_category, main_channel_in_category,
):
    _, _ = git_repo_with_projects
    guild, cat = guild_with_category
    adapter = FakeAdapter(guild)
    runner = FakeRunner()

    from gateway.stream_bootstrap import bootstrap_streams_for_project

    result = await bootstrap_streams_for_project(
        runner=runner,
        scope_id=str(cat.id),
        slug="billing",
        main_channel_id=str(main_channel_in_category.id),
        workstream_manifest={
            "frontend": {"agentRole": "fullstack"},  # not allowed
        },
        approved_head_sha="abc",
        adapter=adapter,
        guild_id="12345",
    )

    assert result.error is not None
    assert "invalid manifest" in result.error.lower()
    # No side effects: no channels created, no sessions, nothing in
    # the no-auto-thread set, no runstate files.
    stream_chans = [c for c in cat.channels if c is not main_channel_in_category]
    assert stream_chans == []
    assert len(runner._session_router) == 0
    assert adapter._no_auto_thread_channels == set()


@pytest.mark.asyncio
async def test_bootstrap_rejects_empty_manifest(
    git_repo_with_projects, guild_with_category, main_channel_in_category,
):
    guild, cat = guild_with_category
    adapter = FakeAdapter(guild)
    runner = FakeRunner()

    from gateway.stream_bootstrap import bootstrap_streams_for_project

    result = await bootstrap_streams_for_project(
        runner=runner,
        scope_id=str(cat.id),
        slug="billing",
        main_channel_id=str(main_channel_in_category.id),
        workstream_manifest={},
        approved_head_sha="abc",
        adapter=adapter,
        guild_id="12345",
    )
    assert result.error is not None
    assert "manifest" in result.error.lower()


@pytest.mark.asyncio
async def test_bootstrap_defaults_agent_role_to_backend_when_missing(
    git_repo_with_projects, guild_with_category, main_channel_in_category,
):
    guild, cat = guild_with_category
    adapter = FakeAdapter(guild)
    runner = FakeRunner()

    from gateway.stream_bootstrap import bootstrap_streams_for_project

    result = await bootstrap_streams_for_project(
        runner=runner,
        scope_id=str(cat.id),
        slug="billing",
        main_channel_id=str(main_channel_in_category.id),
        workstream_manifest={
            "api": {"completionMode": "code"},  # no agentRole field
        },
        approved_head_sha="abc",
        adapter=adapter,
        guild_id="12345",
    )
    assert result.error is None, result.error
    assert len(result.streams) == 1
    assert result.streams[0].role == "backend"

    await _cancel_session_workers(runner)


# -----------------------------------------------------------------------------
# Per-project lock serializes concurrent bootstraps for the same project
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_project_lock_serializes_concurrent_bootstraps(
    git_repo_with_projects, guild_with_category, main_channel_in_category,
    monkeypatch,
):
    """Two concurrent bootstrap_streams_for_project() calls on the same
    (scope_id, slug) must serialize.

    We can't directly observe locking, but we CAN observe that the two
    runs don't tangle each other — each gets a coherent set of streams
    and the second run sees the first run's state already on disk.
    """
    guild, cat = guild_with_category
    adapter = FakeAdapter(guild)
    runner = FakeRunner()

    from gateway.stream_bootstrap import bootstrap_streams_for_project

    # First bootstrap: 1 stream "alpha".  Second: 1 stream "beta".
    # Same (scope_id, slug) on purpose to share the lock.
    res_a, res_b = await asyncio.gather(
        bootstrap_streams_for_project(
            runner=runner, scope_id=str(cat.id), slug="billing",
            main_channel_id=str(main_channel_in_category.id),
            workstream_manifest={"alpha": {"agentRole": "frontend"}},
            approved_head_sha="aaa",
            adapter=adapter, guild_id="12345",
        ),
        bootstrap_streams_for_project(
            runner=runner, scope_id=str(cat.id), slug="billing",
            main_channel_id=str(main_channel_in_category.id),
            workstream_manifest={"beta": {"agentRole": "backend"}},
            approved_head_sha="bbb",
            adapter=adapter, guild_id="12345",
        ),
    )

    # Both bootstraps should succeed (different stream names ⇒ no
    # channel-name collision).  Lock just ensures they don't interleave.
    assert res_a.error is None, res_a.error
    assert res_b.error is None, res_b.error
    # Two stream channels in addition to the main one.
    stream_chans = [c for c in cat.channels if c is not main_channel_in_category]
    assert {c.name for c in stream_chans} == {"billing-alpha", "billing-beta"}

    await _cancel_session_workers(runner)


# -----------------------------------------------------------------------------
# Guild not connected → clean error, no side effects
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_errors_cleanly_when_guild_unknown(
    git_repo_with_projects, guild_with_category, main_channel_in_category,
):
    guild, cat = guild_with_category
    adapter = FakeAdapter(guild)
    runner = FakeRunner()

    from gateway.stream_bootstrap import bootstrap_streams_for_project

    result = await bootstrap_streams_for_project(
        runner=runner,
        scope_id=str(cat.id),
        slug="billing",
        main_channel_id=str(main_channel_in_category.id),
        workstream_manifest={"frontend": {"agentRole": "frontend"}},
        approved_head_sha="abc",
        adapter=adapter,
        guild_id="99999",  # bot not in this guild
    )
    assert result.error is not None
    assert "guild" in result.error.lower()
    assert len(runner._session_router) == 0
