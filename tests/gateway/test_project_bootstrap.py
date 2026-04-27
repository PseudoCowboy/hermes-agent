"""Tests for gateway/project_bootstrap.py (P6).

The bootstrap orchestrator is the entry point for ``!new <requirement>``.
These tests use fakes for the Discord adapter (``_client.get_guild`` /
``get_channel`` / ``fetch_channel``), the guild's
``create_category`` / ``delete``, and the category's
``create_text_channel``.  We never spin up a real discord.py client.

Tests cover:

* successful bootstrap — category + main channel + session + worker
  task created, requirement seeded into the inbox.
* failure rollback at each lifecycle step.
* slug derivation edge cases (empty / non-ascii / overlong).
* bootstrap returns a usage hint on empty requirement.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from gateway.project_bootstrap import (
    SLUG_FALLBACK,
    SLUG_MAX_LEN,
    _slugify,
    bootstrap_new_project,
)
from gateway.session import Platform, SessionSource
from gateway.session_router import SessionRouter


# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------


class FakeChannel:
    def __init__(self, channel_id: int, name: str, topic: str = "", category=None):
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
    def __init__(self, category_id: int, name: str, *, fail_create_channel: bool = False):
        self.id = category_id
        self.name = name
        self.fail_create_channel = fail_create_channel
        self.channels: List[FakeChannel] = []
        self.deleted = False
        self.delete_reason: Optional[str] = None
        self._next_channel_id = category_id + 1

    async def create_text_channel(self, name, topic=None, overwrites=None, reason=None):
        if self.fail_create_channel:
            raise RuntimeError("simulated create_text_channel failure")
        ch = FakeChannel(self._next_channel_id, name, topic or "", category=self)
        self._next_channel_id += 1
        self.channels.append(ch)
        return ch

    async def delete(self, reason=None):
        self.deleted = True
        self.delete_reason = reason


class FakeGuild:
    def __init__(self, guild_id: int, *, fail_create_category: bool = False):
        self.id = guild_id
        self.fail_create_category = fail_create_category
        self.categories: List[FakeCategory] = []
        self._next_category_id = 9000

    async def create_category(self, name, overwrites=None, reason=None):
        if self.fail_create_category:
            raise RuntimeError("simulated create_category failure")
        cat = FakeCategory(self._next_category_id, name)
        self._next_category_id += 1
        self.categories.append(cat)
        return cat


class FakeClient:
    def __init__(self, guild: FakeGuild):
        self._guild = guild
        # Seed an id->channel map updated when channels are created.
        # We refresh lazily in get_channel to pick up channels created
        # mid-test.

    def get_guild(self, gid: int):
        if gid == self._guild.id:
            return self._guild
        return None

    def _all_channels(self):
        chans = {}
        for cat in self._guild.categories:
            chans[cat.id] = cat
            for ch in cat.channels:
                chans[ch.id] = ch
        return chans

    def get_channel(self, cid: int):
        return self._all_channels().get(cid)

    async def fetch_channel(self, cid: int):
        ch = self._all_channels().get(cid)
        if ch is None:
            raise RuntimeError(f"no such channel {cid}")
        return ch


class FakeAdapter:
    def __init__(self, guild: FakeGuild, *, bot_user_id: Optional[int] = 123456):
        self._client = FakeClient(guild)
        self.bot_user_id = bot_user_id
        self.sent: List[tuple] = []
        # Bootstrap registers orchestration channels here so Discord
        # auto-thread doesn't fork @mention replies into a child thread.
        self._no_auto_thread_channels: set = set()

    async def send(self, chat_id, content, **kwargs):
        self.sent.append((chat_id, content))


class FakeRunner:
    """Just enough of GatewayRunner for bootstrap to do its job.

    Bootstrap needs only ``_session_router`` and ``_session_key_for_source``.
    The worker reads ``config`` + ``_resolve_turn_agent_config`` lazily
    when it constructs an AIAgent — we never let the worker run an
    actual turn in these tests, so those attributes are unused.
    """

    def __init__(self):
        self._session_router = SessionRouter()
        self.config = SimpleNamespace(
            group_sessions_per_user=True,
            thread_sessions_per_user=False,
        )
        self.session_store = None

    def _session_key_for_source(self, source):
        # Mirror the production fallback path (no session_store).
        from gateway.session import build_session_key

        return build_session_key(source)


# -----------------------------------------------------------------------------
# Slugify
# -----------------------------------------------------------------------------


def test_slugify_basic():
    assert _slugify("Add feature X") == "add-feature-x"


def test_slugify_truncates():
    long = "a" * 100
    out = _slugify(long)
    assert len(out) <= SLUG_MAX_LEN


def test_slugify_empty_falls_back():
    assert _slugify("") == SLUG_FALLBACK
    assert _slugify("   ") == SLUG_FALLBACK


def test_slugify_non_ascii_falls_back():
    assert _slugify("中文需求") == SLUG_FALLBACK


def test_slugify_collapses_punctuation():
    assert _slugify("foo!!!bar??baz") == "foo-bar-baz"


def test_slugify_strips_leading_trailing_dashes_after_truncation():
    # Construct a string that would truncate at a dash.
    s = "x" * (SLUG_MAX_LEN - 1) + " " + "y"
    out = _slugify(s)
    assert not out.endswith("-")


# -----------------------------------------------------------------------------
# Bootstrap happy path
# -----------------------------------------------------------------------------


@pytest.fixture
def home_source():
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="111",  # home channel id
        chat_type="group",
        user_id="42",
        user_name="alice",
    )


@pytest.mark.asyncio
async def test_bootstrap_creates_category_main_channel_session_and_worker(home_source):
    guild = FakeGuild(guild_id=12345)
    adapter = FakeAdapter(guild)
    runner = FakeRunner()

    reply = await bootstrap_new_project(
        runner=runner,
        requirement="Build a payments dashboard",
        source=home_source,
        adapter=adapter,
        guild_id="12345",
    )

    # Category created with slug.
    assert len(guild.categories) == 1
    cat = guild.categories[0]
    assert cat.name.startswith("build-a-payments")

    # Main channel created inside it.
    assert len(cat.channels) == 1
    main = cat.channels[0]
    assert main.name.endswith("-main")

    # Session registered with persona + slug + main_channel_id.
    assert len(runner._session_router) == 1
    sess = next(iter(runner._session_router._sessions.values()))
    assert sess.persona == "orchestrator"
    assert sess.slug == cat.name
    assert sess.main_channel_id == str(main.id)
    assert sess.scope_id == str(cat.id)

    # Worker spawned.
    assert sess._in_flight is not None
    assert isinstance(sess._in_flight, asyncio.Task)

    # Inbox seeded with the original requirement.
    # (qsize after deliver_message — worker hasn't pulled yet because
    # we never await long enough for asyncio.to_thread to run.)
    assert sess.inbox_qsize() == 1

    # User-facing reply mentions the channel.
    assert "<#" in reply and str(main.id) in reply

    # Tear down: cancel the worker so pytest doesn't warn about
    # pending tasks.  The worker will block on get_next() forever
    # otherwise.  Wrap in try/except to swallow CancelledError.
    sess._in_flight.cancel()
    try:
        await sess._in_flight
    except (asyncio.CancelledError, BaseException):
        pass


# -----------------------------------------------------------------------------
# Bootstrap rollback paths
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_rolls_back_when_main_channel_creation_fails(home_source):
    guild = FakeGuild(guild_id=12345)

    # Patch create_category to return a category that fails on
    # create_text_channel — simulating the second step blowing up.
    real_create = guild.create_category

    async def failing_create_category(name, overwrites=None, reason=None):
        cat = await real_create(name, overwrites=overwrites, reason=reason)
        cat.fail_create_channel = True
        return cat

    guild.create_category = failing_create_category

    adapter = FakeAdapter(guild)
    runner = FakeRunner()

    reply = await bootstrap_new_project(
        runner=runner,
        requirement="Will fail",
        source=home_source,
        adapter=adapter,
        guild_id="12345",
    )

    # Error string returned.
    assert reply.startswith("⚠️ Bootstrap failed")
    # Category was created then rolled back.
    assert len(guild.categories) == 1
    assert guild.categories[0].deleted is True
    # No session left in router.
    assert len(runner._session_router) == 0


@pytest.mark.asyncio
async def test_bootstrap_returns_usage_hint_on_empty_requirement(home_source):
    guild = FakeGuild(guild_id=12345)
    adapter = FakeAdapter(guild)
    runner = FakeRunner()

    reply = await bootstrap_new_project(
        runner=runner,
        requirement="   ",
        source=home_source,
        adapter=adapter,
        guild_id="12345",
    )

    assert reply.startswith("Usage: `!new")
    # No category created, no session registered.
    assert len(guild.categories) == 0
    assert len(runner._session_router) == 0


@pytest.mark.asyncio
async def test_bootstrap_fails_cleanly_when_guild_unknown(home_source):
    guild = FakeGuild(guild_id=12345)
    adapter = FakeAdapter(guild)
    runner = FakeRunner()

    reply = await bootstrap_new_project(
        runner=runner,
        requirement="Anything",
        source=home_source,
        adapter=adapter,
        guild_id="99999",  # bot isn't in this guild
    )

    assert "not connected to guild" in reply
    assert len(guild.categories) == 0
    assert len(runner._session_router) == 0


@pytest.mark.asyncio
async def test_bootstrap_fails_cleanly_when_create_category_raises(home_source):
    guild = FakeGuild(guild_id=12345, fail_create_category=True)
    adapter = FakeAdapter(guild)
    runner = FakeRunner()

    reply = await bootstrap_new_project(
        runner=runner,
        requirement="Anything",
        source=home_source,
        adapter=adapter,
        guild_id="12345",
    )

    assert reply.startswith("⚠️ Bootstrap failed")
    assert len(guild.categories) == 0  # never created
    assert len(runner._session_router) == 0


# -----------------------------------------------------------------------------
# Per-channel session key (regression: was per-user)
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_session_key_is_channel_scoped_not_per_user(home_source):
    """The registered session_key must NOT contain the operator's user_id —
    other participants in the project channel must hit the same session.
    Regression: chat_type='group' + group_sessions_per_user=True would
    make build_session_key append user_id and orphan collaborators.
    """
    guild = FakeGuild(guild_id=12345)
    adapter = FakeAdapter(guild)
    runner = FakeRunner()

    await bootstrap_new_project(
        runner=runner,
        requirement="multi-user project",
        source=home_source,  # user_id="42"
        adapter=adapter,
        guild_id="12345",
    )

    assert len(runner._session_router) == 1
    key = next(iter(runner._session_router._sessions.keys()))
    # The key must not include the operator's user_id, otherwise other
    # users' messages would miss the session.
    assert "42" not in key.split(":"), (
        f"session key {key!r} contains the operator's user_id — "
        "this would isolate the session per user instead of per channel"
    )

    # Tear down the worker so pytest doesn't warn.
    sess = next(iter(runner._session_router._sessions.values()))
    if sess._in_flight:
        sess._in_flight.cancel()
        try:
            await sess._in_flight
        except (asyncio.CancelledError, BaseException):
            pass


@pytest.mark.asyncio
async def test_seed_event_carries_operator_user_id(home_source):
    """Even though the session_key is channel-scoped, the synthesized
    initial requirement event must carry the operator's user_id so the
    worker can use it as the approver_user_id for
    ``discord_wait_for_reaction``.  Without this, a clear requirement
    going straight to plan approval would leave user_id unbound.

    Regression for Codex follow-up High #2.
    """
    guild = FakeGuild(guild_id=12345)
    adapter = FakeAdapter(guild)
    runner = FakeRunner()

    await bootstrap_new_project(
        runner=runner,
        requirement="ship the thing",
        source=home_source,  # user_id="42"
        adapter=adapter,
        guild_id="12345",
    )

    sess = next(iter(runner._session_router._sessions.values()))
    assert sess.inbox_qsize() == 1
    seeded = await sess.get_next()
    src = getattr(seeded.event, "source", None)
    assert src is not None
    assert getattr(src, "user_id", None) == "42", (
        "seed event lost the operator user_id — approver auto-bind "
        "would not work for plans approved on the very first turn"
    )

    # Tear down the worker.
    if sess._in_flight:
        sess._in_flight.cancel()
        try:
            await sess._in_flight
        except (asyncio.CancelledError, BaseException):
            pass


# -----------------------------------------------------------------------------
# Rollback: channel created then later step fails → both deleted
# -----------------------------------------------------------------------------


class _FailRegisterRouter(SessionRouter):
    """SessionRouter whose register() always raises — simulates the
    bootstrap failing AFTER the main channel is created."""

    def register(self, key, session):  # type: ignore[override]
        raise RuntimeError("simulated session register failure")


@pytest.mark.asyncio
async def test_bootstrap_rolls_back_channel_and_category_when_register_fails(home_source):
    """If session registration fails (after both category and channel
    have been created), rollback must delete BOTH the channel and the
    category — not just the category.  Some discord.py backends don't
    cascade child deletes on a non-empty category."""
    guild = FakeGuild(guild_id=12345)
    adapter = FakeAdapter(guild)
    runner = FakeRunner()
    runner._session_router = _FailRegisterRouter()

    reply = await bootstrap_new_project(
        runner=runner,
        requirement="will fail at register",
        source=home_source,
        adapter=adapter,
        guild_id="12345",
    )

    assert reply.startswith("⚠️ Bootstrap failed")
    # Category was created and rolled back.
    assert len(guild.categories) == 1
    cat = guild.categories[0]
    assert cat.deleted is True
    # Channel was also deleted explicitly during rollback (not relying
    # on cascade).  Verifying both layers were torn down means a future
    # change that drops the explicit channel-delete would regress here.
    assert len(cat.channels) == 1
    assert cat.channels[0].deleted is True, (
        "main channel was not explicitly deleted during rollback — "
        "category-only delete is not safe across all discord.py backends"
    )
    assert len(runner._session_router) == 0


# -----------------------------------------------------------------------------
# Discord auto-thread suppression for orchestrator main channel
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_registers_main_channel_in_no_auto_thread_set(home_source):
    """Bootstrap must register the new {slug}-main channel into the
    adapter's no-auto-thread set so that Discord auto-thread does NOT
    fork the bot's @mention reply into a child thread (which would
    have a different chat_id than the channel-scoped session_key).

    Regression for Codex follow-up: without this, every operator
    @mention in {slug}-main would land on a thread the long-lived
    session can't see.
    """
    guild = FakeGuild(guild_id=12345)
    adapter = FakeAdapter(guild)
    runner = FakeRunner()

    await bootstrap_new_project(
        runner=runner,
        requirement="auto-thread suppress test",
        source=home_source,
        adapter=adapter,
        guild_id="12345",
    )

    sess = next(iter(runner._session_router._sessions.values()))
    assert sess.main_channel_id in adapter._no_auto_thread_channels, (
        f"main channel {sess.main_channel_id} was not added to "
        f"adapter._no_auto_thread_channels — auto-thread will fork "
        f"the bot's reply into a child thread the session can't see"
    )

    # Tear down the worker.
    if sess._in_flight:
        sess._in_flight.cancel()
        try:
            await sess._in_flight
        except (asyncio.CancelledError, BaseException):
            pass


@pytest.mark.asyncio
async def test_bootstrap_rollback_drops_no_auto_thread_entry(home_source):
    """If bootstrap fails after registering the main channel into the
    no-auto-thread set, rollback must drop the entry — otherwise stale
    ids accumulate on the adapter forever."""
    guild = FakeGuild(guild_id=12345)
    adapter = FakeAdapter(guild)
    runner = FakeRunner()
    runner._session_router = _FailRegisterRouter()

    await bootstrap_new_project(
        runner=runner,
        requirement="rollback no-thread test",
        source=home_source,
        adapter=adapter,
        guild_id="12345",
    )

    # Bootstrap failed → no_auto_thread set should be empty.
    assert adapter._no_auto_thread_channels == set(), (
        "rollback did not drop the no-auto-thread entry; stale ids "
        f"left behind: {adapter._no_auto_thread_channels}"
    )

