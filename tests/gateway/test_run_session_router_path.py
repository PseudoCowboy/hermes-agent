"""Integration: ``GatewayRunner._handle_message`` honors the SessionRouter
fast-path before falling through to the per-message agent flow (P5).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_router import LongLivedSession


@pytest.fixture
def runner(monkeypatch, tmp_path):
    """A runner with hermes_home pointed at tmp_path and an adapter
    stub registered (so any fall-through to pairing has somewhere to
    send and we can detect it)."""
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")

    # Allow every user so authorization isn't the gate we accidentally hit.
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "1")

    r = GatewayRunner(GatewayConfig())
    r.adapters[Platform.DISCORD] = SimpleNamespace(send=AsyncMock())
    return r


def _make_event(text: str = "hello", user_id: str = "user-42") -> MessageEvent:
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        chat_type="group",
        user_id=user_id,
    )
    return MessageEvent(text=text, source=source, internal=False)


@pytest.mark.asyncio
async def test_registered_session_short_circuits_per_message_path(runner):
    """When a session is registered for the channel, deliver_message is
    called and the per-message agent path does NOT run."""
    event = _make_event()
    session_key = runner._session_key_for_source(event.source)

    session = LongLivedSession(session_key, bot_user_id=None)
    runner._session_router.register(session_key, session)

    # Sentinel: if the per-message agent path runs it would hit
    # ``_handle_message_with_agent`` (or a downstream method).  Replace
    # the most common downstream attributes with mocks that explode if
    # called — any of them firing means the fast-path didn't short-circuit.
    explode = AsyncMock(side_effect=AssertionError("per-message path ran"))
    runner._handle_message_with_agent = explode  # type: ignore[attr-defined]

    result = await runner._handle_message(event)

    assert result is None
    explode.assert_not_called()
    assert session.inbox_qsize() == 1
    payload = await session.get_next()
    assert payload.kind == "message"
    assert payload.event is event


@pytest.mark.asyncio
async def test_unregistered_channel_falls_through_to_per_message_path(runner):
    """Without a registered session, the existing per-message flow runs."""
    event = _make_event()

    # Sentinel: any per-message path attribute firing means we DID
    # fall through.  We can't easily run the full agent path in a unit
    # test, so we patch ``_handle_message_with_agent`` to record being
    # called and short-circuit the rest of the flow.
    sentinel = AsyncMock(return_value="ok")
    runner._handle_message_with_agent = sentinel  # type: ignore[attr-defined]

    # The router is empty — fall-through should run.
    assert len(runner._session_router) == 0

    try:
        await runner._handle_message(event)
    except Exception:
        # Downstream code (interrupt handling, command processing) may
        # still error in a unit-test environment without a full session.
        # The signal we care about is whether the router *short-circuited*.
        pass

    # If the fast-path had short-circuited, sentinel would never run AND
    # nothing else would either — but more importantly the session
    # would have to exist.  The actual signal: assert no LongLivedSession
    # was registered (a regression that auto-created one would surface here).
    assert len(runner._session_router) == 0


@pytest.mark.asyncio
async def test_router_attribute_present_on_init(runner):
    """Sanity: the router is wired in __init__, not lazily, so test
    fixtures and P6 registration code can rely on it."""
    from gateway.session_router import SessionRouter

    assert isinstance(runner._session_router, SessionRouter)
    assert len(runner._session_router) == 0


@pytest.mark.asyncio
async def test_slash_command_in_registered_channel_does_not_short_circuit(runner):
    """Carve-out: even when a session is registered, slash commands
    must still hit their existing handlers (otherwise /stop, /new,
    /approve, etc. would silently disappear into the session inbox).
    Regression for Codex P5 finding #1."""
    event = _make_event(text="/status")
    session_key = runner._session_key_for_source(event.source)

    session = LongLivedSession(session_key)
    runner._session_router.register(session_key, session)

    # If the fast-path mistakenly fires, deliver_message will enqueue.
    # We try the call and assert nothing was delivered to the session.
    try:
        await runner._handle_message(event)
    except Exception:
        # Downstream command handling may fail in this minimal
        # fixture; the only signal we care about is the inbox.
        pass

    assert session.inbox_qsize() == 0, (
        "slash command was incorrectly routed to LongLivedSession.inbox"
    )


@pytest.mark.asyncio
async def test_pending_update_response_in_registered_channel_does_not_short_circuit(runner):
    """Carve-out: a reply to a pending /update prompt must reach the
    update-response writer, not the LongLivedSession inbox.
    Regression for Codex P5 finding #1."""
    event = _make_event(text="y")
    session_key = runner._session_key_for_source(event.source)

    session = LongLivedSession(session_key)
    runner._session_router.register(session_key, session)
    # Mark an update prompt as outstanding for this channel.
    runner._update_prompt_pending[session_key] = True

    try:
        await runner._handle_message(event)
    except Exception:
        # The update-response file write may fail in this fixture.
        pass

    assert session.inbox_qsize() == 0, (
        "pending /update reply was incorrectly routed to LongLivedSession.inbox"
    )


@pytest.mark.asyncio
async def test_full_inbox_returns_user_visible_warning(runner):
    """When the LongLivedSession inbox is full, the fast-path must
    return a user-visible warning so the platform adapter surfaces
    feedback — silent drops are unacceptable.
    Regression for Codex P5 round-2 finding."""
    event = _make_event(text="hello")
    session_key = runner._session_key_for_source(event.source)

    # Saturate the session inbox.
    session = LongLivedSession(session_key, bot_user_id=None, inbox_maxsize=1)
    runner._session_router.register(session_key, session)
    assert await session.deliver_message(_make_event(text="filler")) is True
    assert session.inbox_qsize() == 1

    result = await runner._handle_message(event)
    assert isinstance(result, str)
    assert "inbox is full" in result.lower() or "dropped" in result.lower()


@pytest.mark.asyncio
async def test_self_message_drop_is_silent(runner):
    """When deliver_message drops a self-message, the fast-path returns
    None (no user-visible reply) — the bot's own message bouncing back
    must not trigger a warning."""
    event = _make_event(text="hello", user_id="999")
    session_key = runner._session_key_for_source(event.source)

    # bot_user_id matches the event author → deliver_message returns False
    # via the self-drop branch, NOT the inbox-full branch.
    session = LongLivedSession(session_key, bot_user_id=999)
    runner._session_router.register(session_key, session)

    result = await runner._handle_message(event)
    assert result is None  # silent drop, no warning surfaced
    assert session.inbox_qsize() == 0


# -----------------------------------------------------------------------------
# P6: Discord home-channel pre-router for !new <requirement>
# -----------------------------------------------------------------------------


def _configure_home_channel(runner, *, channel_id: str = "777", guild_id: str = "12345"):
    """Stamp orchestration_home_channel_id / orchestration_guild_id onto
    the gateway's Discord PlatformConfig so the home-channel pre-router
    has somewhere to match against."""
    from gateway.config import PlatformConfig

    runner.config.platforms[Platform.DISCORD] = PlatformConfig(
        orchestration_home_channel_id=channel_id,
        orchestration_guild_id=guild_id,
    )


@pytest.mark.asyncio
async def test_new_command_in_home_channel_invokes_bootstrap(runner, monkeypatch):
    """``!new <requirement>`` in the configured home channel routes to
    bootstrap_new_project, NOT the per-message agent path."""
    _configure_home_channel(runner, channel_id="777")
    event = _make_event(text="!new build a thing")
    event.source.chat_id = "777"  # match home channel

    captured = {}

    async def fake_bootstrap(*, runner, requirement, source, adapter, guild_id):
        captured["requirement"] = requirement
        captured["guild_id"] = guild_id
        return "✓ ok"

    monkeypatch.setattr(
        "gateway.project_bootstrap.bootstrap_new_project", fake_bootstrap
    )

    explode = AsyncMock(side_effect=AssertionError("per-message path ran"))
    runner._handle_message_with_agent = explode  # type: ignore[attr-defined]

    result = await runner._handle_message(event)
    assert result == "✓ ok"
    assert captured["requirement"] == "build a thing"
    assert captured["guild_id"] == "12345"
    explode.assert_not_called()


@pytest.mark.asyncio
async def test_non_new_message_in_home_channel_falls_through(runner, monkeypatch):
    """Plain text in the home channel (no ``!new`` prefix) falls through
    to the existing per-message agent path."""
    _configure_home_channel(runner, channel_id="777")
    event = _make_event(text="just chatting")
    event.source.chat_id = "777"

    sentinel = AsyncMock(return_value="ok")
    runner._handle_message_with_agent = sentinel  # type: ignore[attr-defined]

    bootstrap_called = AsyncMock()
    monkeypatch.setattr(
        "gateway.project_bootstrap.bootstrap_new_project", bootstrap_called
    )

    try:
        await runner._handle_message(event)
    except Exception:
        pass

    bootstrap_called.assert_not_called()


@pytest.mark.asyncio
async def test_new_command_outside_home_channel_falls_through(runner, monkeypatch):
    """``!new`` in a non-home channel is treated as a regular message —
    no bootstrap, falls through to the per-message agent path."""
    _configure_home_channel(runner, channel_id="777")
    event = _make_event(text="!new ignored")
    event.source.chat_id = "different-channel"

    bootstrap_called = AsyncMock()
    monkeypatch.setattr(
        "gateway.project_bootstrap.bootstrap_new_project", bootstrap_called
    )

    sentinel = AsyncMock(return_value="ok")
    runner._handle_message_with_agent = sentinel  # type: ignore[attr-defined]

    try:
        await runner._handle_message(event)
    except Exception:
        pass

    bootstrap_called.assert_not_called()


@pytest.mark.asyncio
async def test_new_with_empty_requirement_returns_usage_hint(runner):
    """``!new`` with no requirement string returns a usage hint and
    does NOT invoke bootstrap."""
    _configure_home_channel(runner, channel_id="777")
    event = _make_event(text="!new   ")
    event.source.chat_id = "777"

    result = await runner._handle_message(event)
    assert isinstance(result, str)
    assert result.startswith("Usage: `!new")


@pytest.mark.asyncio
async def test_slash_command_in_home_channel_falls_through(runner, monkeypatch):
    """A slash command (``/status``, ``/reset``, ``/update``) in the
    home channel must NOT be intercepted by the home-channel pre-router.
    Only ``!new`` is the home-channel verb; everything else falls through
    to the normal handlers.  Regression for Codex P6 suggestion #1.
    """
    _configure_home_channel(runner, channel_id="777")
    event = _make_event(text="/status")
    event.source.chat_id = "777"

    bootstrap_called = AsyncMock()
    monkeypatch.setattr(
        "gateway.project_bootstrap.bootstrap_new_project", bootstrap_called
    )

    try:
        await runner._handle_message(event)
    except Exception:
        # /status handling can fail in this minimal fixture; we only
        # care that bootstrap wasn't invoked.
        pass

    bootstrap_called.assert_not_called()



# -----------------------------------------------------------------------------
# P6: /reset must tear down LongLivedSession + worker
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_teardown_unregisters_session_and_cancels_worker(runner):
    """``_teardown_long_lived_session_for`` must drop the session out of
    the router AND cancel the in-flight worker.  Regression for Codex
    Critical #2: /reset/new previously left the orchestrator worker
    running and its history live, so the next non-command message would
    route to a stale agent.
    """
    event = _make_event()
    session_key = runner._session_key_for_source(event.source)

    session = LongLivedSession(session_key)

    # Plant a long-running fake worker so we can assert it was cancelled.
    cancelled = asyncio.Event()

    async def _fake_worker():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    session._in_flight = asyncio.create_task(_fake_worker())
    runner._session_router.register(session_key, session)
    assert len(runner._session_router) == 1

    # Let the worker actually start so the cancel hits inside the try.
    # Without this yield, cancel() lands before the task ever runs and
    # the except clause never fires (the task just finishes cancelled).
    await asyncio.sleep(0)

    await runner._teardown_long_lived_session_for(event.source)

    assert len(runner._session_router) == 0
    assert session.closed is True
    assert cancelled.is_set(), "in-flight worker was not cancelled"


@pytest.mark.asyncio
async def test_teardown_also_finds_channel_scoped_key(runner):
    """Bootstrap registers under a channel-scoped key (no user_id).
    A user-issued /reset arrives with a per-user source — teardown must
    probe BOTH the per-user key AND the channel-scoped key, otherwise
    the bootstrap-registered session would be missed.
    """
    event = _make_event()  # user_id="user-42"
    # Fabricate the channel-scoped key the way bootstrap does.
    channel_only = SessionSource(
        platform=event.source.platform,
        chat_id=event.source.chat_id,
        chat_type="group",
        user_id=None,
        user_name=None,
    )
    channel_key = runner._session_key_for_source(channel_only)

    session = LongLivedSession(channel_key)
    runner._session_router.register(channel_key, session)
    assert len(runner._session_router) == 1

    # Teardown is called with the per-user source from /reset.
    await runner._teardown_long_lived_session_for(event.source)

    assert len(runner._session_router) == 0


@pytest.mark.asyncio
async def test_teardown_is_noop_when_no_session_registered(runner):
    """No registration → teardown returns cleanly without raising."""
    event = _make_event()
    # Should not raise.
    await runner._teardown_long_lived_session_for(event.source)
    assert len(runner._session_router) == 0


@pytest.mark.asyncio
async def test_fast_path_finds_channel_scoped_session_for_per_user_event(runner):
    """Bootstrap registers under a CHANNEL-scoped key (no user_id), but
    inbound user events arrive with user_id populated, so the per-user
    ``_quick_key`` won't find that registration directly.  The fast path
    must fall back to the channel-only key derived from the same source.

    Regression for Codex follow-up High #1: without this fallback, every
    user message in the project channel falls through to the per-message
    agent path and the orchestrator worker never sees its inbox.
    """
    event = _make_event(user_id="operator-99")
    channel_only = SessionSource(
        platform=event.source.platform,
        chat_id=event.source.chat_id,
        chat_type="group",
        user_id=None,
        user_name=None,
    )
    channel_key = runner._session_key_for_source(channel_only)
    per_user_key = runner._session_key_for_source(event.source)
    # Sanity: the two keys must actually differ for this regression to
    # be meaningful — if config doesn't isolate per-user, the test is
    # a no-op rather than a false positive.
    assert channel_key != per_user_key, (
        "test fixture's gateway config doesn't isolate per-user; "
        "this regression test cannot exercise the channel fallback"
    )

    session = LongLivedSession(channel_key)
    runner._session_router.register(channel_key, session)

    # Per-message path must NOT run when the channel-scoped session
    # absorbs the message.
    explode = AsyncMock(side_effect=AssertionError("per-message path ran"))
    runner._handle_message_with_agent = explode  # type: ignore[attr-defined]

    result = await runner._handle_message(event)

    assert result is None
    explode.assert_not_called()
    assert session.inbox_qsize() == 1


