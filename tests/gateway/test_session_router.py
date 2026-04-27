"""Tests for ``gateway.session_router`` (P5).

Cover:

- ``deliver_message`` admission state machine (None / SENTINEL / int).
- Self-message drop based on ``bot_user_id``.
- Concurrent deliveries serialise so a clarification is admitted
  exactly once even under contention.
- ``mark_prompt_*`` / ``clear_pending`` transitions.
- ``SessionRouter`` register / unregister / get.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from gateway.session_router import (
    DEFAULT_INBOX_MAXSIZE,
    InboundMessage,
    LongLivedSession,
    SENTINEL_PENDING,
    SessionRouter,
)


# =============================================================================
# Test fakes
# =============================================================================


@dataclass
class _FakeEvent:
    """Minimal stand-in for ``gateway.run.MessageEvent``.

    The real event has ``event.source.user_id``; tests use the simpler
    top-level ``author_id`` shape supported by ``_extract_author_id``.
    """

    author_id: int
    text: str = "hi"


@dataclass
class _FakeSource:
    user_id: int


@dataclass
class _FakeEventWithSource:
    """Variant that mimics the real ``MessageEvent.source.user_id`` shape."""

    source: _FakeSource
    text: str = "hi"


# =============================================================================
# deliver_message — admission state machine
# =============================================================================


@pytest.mark.asyncio
async def test_deliver_buffers_when_no_pending():
    session = LongLivedSession("ch:1", bot_user_id=999)

    accepted = await session.deliver_message(_FakeEvent(author_id=42))

    assert accepted is True
    assert session.inbox_qsize() == 1
    payload = await session.get_next()
    assert payload.kind == "message"
    assert payload.prompt_message_id is None


@pytest.mark.asyncio
async def test_deliver_drops_self_message():
    session = LongLivedSession("ch:1", bot_user_id=999)

    accepted = await session.deliver_message(_FakeEvent(author_id=999))

    assert accepted is False
    assert session.inbox_qsize() == 0


@pytest.mark.asyncio
async def test_deliver_extracts_author_from_source_shape():
    """Real ``MessageEvent`` exposes author via ``event.source.user_id``."""
    session = LongLivedSession("ch:1", bot_user_id=999)
    event = _FakeEventWithSource(source=_FakeSource(user_id=999))

    accepted = await session.deliver_message(event)

    assert accepted is False  # self-drop via source.user_id


@pytest.mark.asyncio
async def test_deliver_admits_when_no_bot_user_id_set():
    """Without ``bot_user_id`` configured, every event passes the self-drop check."""
    session = LongLivedSession("ch:1")  # bot_user_id = None

    accepted = await session.deliver_message(_FakeEvent(author_id=999))

    assert accepted is True


@pytest.mark.asyncio
async def test_admission_concrete_pending_consumes_exactly_one():
    session = LongLivedSession("ch:1", bot_user_id=999)
    await session.mark_prompt_pending()
    await session.mark_prompt_posted(100)
    assert session.pending_prompt_state == 100

    # First delivery → admitted as clarification answer.
    accepted = await session.deliver_message(_FakeEvent(author_id=42, text="yes"))
    assert accepted is True
    assert session.pending_prompt_state is None  # transitioned back

    first = await session.get_next()
    assert first.kind == "clarification_reply"
    assert first.prompt_message_id == 100

    # Second delivery → plain message, NOT another clarification.
    accepted = await session.deliver_message(_FakeEvent(author_id=42, text="follow-up"))
    assert accepted is True

    second = await session.get_next()
    assert second.kind == "message"
    assert second.prompt_message_id is None


@pytest.mark.asyncio
async def test_sentinel_window_buffers_then_admits_after_post():
    """Messages arriving in the sentinel window must NOT be eaten as the answer."""
    session = LongLivedSession("ch:1", bot_user_id=999)
    await session.mark_prompt_pending()
    assert session.pending_prompt_state is SENTINEL_PENDING

    # User races the bot's post — sentinel window message must buffer.
    accepted = await session.deliver_message(_FakeEvent(author_id=42, text="early"))
    assert accepted is True
    assert session.pending_prompt_state is SENTINEL_PENDING  # still pending

    early = await session.get_next()
    assert early.kind == "message"  # plain, NOT clarification_reply

    # Now the prompt actually posts; the next message is the answer.
    await session.mark_prompt_posted(123)
    accepted = await session.deliver_message(_FakeEvent(author_id=42, text="yes"))
    assert accepted is True
    assert session.pending_prompt_state is None

    answer = await session.get_next()
    assert answer.kind == "clarification_reply"
    assert answer.prompt_message_id == 123


# =============================================================================
# Concurrent deliveries serialise
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_deliveries_admit_exactly_one_clarification():
    """Two concurrent deliveries during an open clarification must
    not both be admitted as the answer.

    Without the lock, both would observe ``pending == 100``, both
    would write their payload as ``clarification_reply``, and the
    first transition to ``None`` would race the second's read.
    """
    session = LongLivedSession("ch:1", bot_user_id=999)
    await session.mark_prompt_pending()
    await session.mark_prompt_posted(100)

    results = await asyncio.gather(
        session.deliver_message(_FakeEvent(author_id=42, text="a")),
        session.deliver_message(_FakeEvent(author_id=43, text="b")),
    )

    assert all(results)  # both accepted into the inbox
    payloads = []
    while session.inbox_qsize():
        payloads.append(await session.get_next())

    kinds = [p.kind for p in payloads]
    # Exactly one is the clarification answer; the other is a plain
    # message buffered after the state transitioned back to None.
    assert kinds.count("clarification_reply") == 1
    assert kinds.count("message") == 1


# =============================================================================
# Pending-prompt state machine
# =============================================================================


@pytest.mark.asyncio
async def test_mark_prompt_posted_requires_sentinel_state():
    """Calling mark_prompt_posted from None or concrete must raise."""
    session = LongLivedSession("ch:1")

    # From None — misuse.
    with pytest.raises(RuntimeError, match="SENTINEL_PENDING"):
        await session.mark_prompt_posted(100)

    # From concrete int — also misuse (caller must clear first).
    await session.mark_prompt_pending()
    await session.mark_prompt_posted(100)
    with pytest.raises(RuntimeError, match="SENTINEL_PENDING"):
        await session.mark_prompt_posted(200)


@pytest.mark.asyncio
async def test_mark_prompt_posted_rejects_non_int():
    session = LongLivedSession("ch:1")
    await session.mark_prompt_pending()
    with pytest.raises(TypeError):
        await session.mark_prompt_posted("100")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_clear_pending_resets_from_any_state():
    session = LongLivedSession("ch:1")

    # From None.
    await session.clear_pending()
    assert session.pending_prompt_state is None

    # From SENTINEL.
    await session.mark_prompt_pending()
    await session.clear_pending()
    assert session.pending_prompt_state is None

    # From concrete int.
    await session.mark_prompt_pending()
    await session.mark_prompt_posted(99)
    await session.clear_pending()
    assert session.pending_prompt_state is None


@pytest.mark.asyncio
async def test_set_bot_user_id_late_bind():
    session = LongLivedSession("ch:1")
    assert session.bot_user_id is None
    session.set_bot_user_id(777)
    assert session.bot_user_id == 777
    # Self-drop now active.
    accepted = await session.deliver_message(_FakeEvent(author_id=777))
    assert accepted is False


# =============================================================================
# Bounded inbox
# =============================================================================


@pytest.mark.asyncio
async def test_full_inbox_drops_with_warning(caplog):
    session = LongLivedSession("ch:1", inbox_maxsize=2)

    assert await session.deliver_message(_FakeEvent(author_id=1)) is True
    assert await session.deliver_message(_FakeEvent(author_id=2)) is True
    # Third overflows.
    with caplog.at_level("WARNING"):
        accepted = await session.deliver_message(_FakeEvent(author_id=3))
    assert accepted is False
    assert any("inbox full" in r.message for r in caplog.records)
    assert session.inbox_qsize() == 2


@pytest.mark.asyncio
async def test_full_inbox_during_clarification_preserves_pending_state():
    """If the inbox is full when a clarification reply arrives, the
    reply is dropped — but the pending state must NOT be cleared, so
    the session still knows it owes an answer.

    Regression for Codex P5 finding #2.
    """
    session = LongLivedSession("ch:1", inbox_maxsize=1)
    # Saturate the inbox.
    assert await session.deliver_message(_FakeEvent(author_id=1)) is True
    assert session.inbox_qsize() == 1

    await session.mark_prompt_pending()
    await session.mark_prompt_posted(100)
    assert session.pending_prompt_state == 100

    # Reply to the clarification arrives while inbox is full.
    accepted = await session.deliver_message(_FakeEvent(author_id=2, text="yes"))
    assert accepted is False
    # Pending state preserved — session still owes the user an answer.
    assert session.pending_prompt_state == 100


# =============================================================================
# SessionRouter
# =============================================================================


def test_router_get_unknown_returns_none():
    router = SessionRouter()
    assert router.get("missing") is None
    assert "missing" not in router
    assert len(router) == 0


def test_router_register_and_get():
    router = SessionRouter()
    s = LongLivedSession("ch:1")
    router.register("ch:1", s)
    assert router.get("ch:1") is s
    assert "ch:1" in router
    assert len(router) == 1


def test_router_unregister_returns_session_then_none():
    router = SessionRouter()
    s = LongLivedSession("ch:1")
    router.register("ch:1", s)
    assert router.unregister("ch:1") is s
    assert router.unregister("ch:1") is None
    assert "ch:1" not in router


def test_router_register_replaces_prior():
    """Re-registering the same key replaces (caller's responsibility to
    tear down the prior session first)."""
    router = SessionRouter()
    s1 = LongLivedSession("ch:1")
    s2 = LongLivedSession("ch:1")
    router.register("ch:1", s1)
    router.register("ch:1", s2)
    assert router.get("ch:1") is s2


def test_inbound_message_dataclass_defaults():
    """Pin the public payload shape — P6 agent loop reads ``kind``."""
    payload = InboundMessage(kind="message", event=object())
    assert payload.kind == "message"
    assert payload.prompt_message_id is None
