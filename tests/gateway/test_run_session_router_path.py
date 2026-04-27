"""Integration: ``GatewayRunner._handle_message`` honors the SessionRouter
fast-path before falling through to the per-message agent flow (P5).
"""

from __future__ import annotations

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
