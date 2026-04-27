"""Tests for gateway.platforms.discord_orchestration.

These exercise the framework-agnostic reaction waiter and admin
helpers using fakes — no live Discord connection.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest

from gateway.platforms.discord_orchestration import (
    DEFAULT_APPROVAL_EMOJIS,
    OrchestrationError,
    ReactionKey,
    ReactionWaiter,
    archive_project_category,
    create_project_category,
    create_stream_channel,
    install_reaction_handler,
)


# =============================================================================
# Fakes
# =============================================================================


@dataclass
class _FakeChannel:
    id: int
    name: str
    delete_calls: list = field(default_factory=list)
    delete_should_raise: Optional[Exception] = None

    async def delete(self, *, reason: Optional[str] = None) -> None:
        if self.delete_should_raise is not None:
            raise self.delete_should_raise
        self.delete_calls.append(reason)


@dataclass
class _FakeCategory:
    id: int
    name: str
    channels: list = field(default_factory=list)
    delete_calls: list = field(default_factory=list)
    create_text_channel_should_raise: Optional[Exception] = None
    delete_should_raise: Optional[Exception] = None

    async def create_text_channel(
        self,
        name: str,
        *,
        topic: Optional[str] = None,
        overwrites: Optional[Dict[Any, Any]] = None,
        reason: Optional[str] = None,
    ) -> _FakeChannel:
        if self.create_text_channel_should_raise is not None:
            raise self.create_text_channel_should_raise
        ch = _FakeChannel(id=10000 + len(self.channels), name=name)
        self.channels.append(ch)
        return ch

    async def delete(self, *, reason: Optional[str] = None) -> None:
        if self.delete_should_raise is not None:
            raise self.delete_should_raise
        self.delete_calls.append(reason)


@dataclass
class _FakeGuild:
    id: int = 1
    create_category_should_raise: Optional[Exception] = None
    categories: list = field(default_factory=list)

    async def create_category(
        self,
        name: str,
        *,
        overwrites: Optional[Dict[Any, Any]] = None,
        reason: Optional[str] = None,
    ) -> _FakeCategory:
        if self.create_category_should_raise is not None:
            raise self.create_category_should_raise
        cat = _FakeCategory(id=2000 + len(self.categories), name=name)
        self.categories.append(cat)
        return cat


class _FakeClient:
    """Minimal stand-in for a discord.py Client/Bot."""

    def __init__(self) -> None:
        self.handlers: Dict[str, Any] = {}

    def event(self, coro):
        # discord.py's @client.event registers the coro under its name.
        self.handlers[coro.__name__] = coro
        return coro


@dataclass
class _FakePayload:
    user_id: int
    channel_id: int
    message_id: int
    emoji: Any


# =============================================================================
# ReactionWaiter
# =============================================================================


class TestReactionKey:
    def test_normalizes_to_strings(self):
        k = ReactionKey.of(1, 2, 3)
        assert k == ReactionKey("1", "2", "3")

    def test_hashable_and_equal(self):
        a = ReactionKey.of(1, 2, 3)
        b = ReactionKey.of("1", "2", "3")
        assert a == b
        assert hash(a) == hash(b)


class TestReactionWaiter:
    @pytest.mark.asyncio
    async def test_resolves_on_matching_emoji(self):
        w = ReactionWaiter()

        async def deliver_later():
            await asyncio.sleep(0.01)
            assert w.deliver(
                channel_id=10, message_id=20, user_id=30, emoji="\u2705"
            )

        asyncio.create_task(deliver_later())
        emoji = await w.wait(10, 20, 30, timeout=1.0)
        assert emoji == "\u2705"
        assert w.pending_count() == 0

    @pytest.mark.asyncio
    async def test_ignores_wrong_user(self):
        w = ReactionWaiter()
        # Wait for user 30; deliver from user 99 — should not resolve.
        wait_task = asyncio.create_task(w.wait(10, 20, 30, timeout=0.1))
        await asyncio.sleep(0.01)
        accepted = w.deliver(
            channel_id=10, message_id=20, user_id=99, emoji="\u2705"
        )
        assert accepted is False
        with pytest.raises(asyncio.TimeoutError):
            await wait_task

    @pytest.mark.asyncio
    async def test_ignores_wrong_channel(self):
        w = ReactionWaiter()
        wait_task = asyncio.create_task(w.wait(10, 20, 30, timeout=0.1))
        await asyncio.sleep(0.01)
        assert not w.deliver(
            channel_id=999, message_id=20, user_id=30, emoji="\u2705"
        )
        with pytest.raises(asyncio.TimeoutError):
            await wait_task

    @pytest.mark.asyncio
    async def test_ignores_unallowed_emoji(self):
        w = ReactionWaiter()
        wait_task = asyncio.create_task(
            w.wait(10, 20, 30, allowed_emojis=["\u2705"], timeout=0.1)
        )
        await asyncio.sleep(0.01)
        assert not w.deliver(
            channel_id=10, message_id=20, user_id=30, emoji="\U0001f44d"  # 👍
        )
        with pytest.raises(asyncio.TimeoutError):
            await wait_task

    @pytest.mark.asyncio
    async def test_timeout_clears_pending(self):
        w = ReactionWaiter()
        with pytest.raises(asyncio.TimeoutError):
            await w.wait(10, 20, 30, timeout=0.05)
        assert w.pending_count() == 0

    @pytest.mark.asyncio
    async def test_duplicate_wait_rejected(self):
        w = ReactionWaiter()
        # Start one wait that will time out, then try to register a
        # duplicate while it's still pending.
        wait_task = asyncio.create_task(w.wait(10, 20, 30, timeout=0.1))
        await asyncio.sleep(0.01)
        with pytest.raises(OrchestrationError, match="already pending"):
            await w.wait(10, 20, 30, timeout=0.01)
        with pytest.raises(asyncio.TimeoutError):
            await wait_task

    @pytest.mark.asyncio
    async def test_cross_session_isolation(self):
        """Same user reacting in two channels must not cross-resolve."""
        w = ReactionWaiter()
        t1 = asyncio.create_task(w.wait(10, 100, 30, timeout=1.0))
        t2 = asyncio.create_task(w.wait(11, 200, 30, timeout=1.0))
        await asyncio.sleep(0.01)
        assert w.deliver(channel_id=10, message_id=100, user_id=30, emoji="\u2705")
        # t1 resolved, t2 still pending.
        assert (await t1) == "\u2705"
        assert w.pending_count() == 1
        assert w.deliver(channel_id=11, message_id=200, user_id=30, emoji="\u274c")
        assert (await t2) == "\u274c"

    @pytest.mark.asyncio
    async def test_default_emojis_used_when_unspecified(self):
        w = ReactionWaiter()
        wait_task = asyncio.create_task(w.wait(10, 20, 30, timeout=1.0))
        await asyncio.sleep(0.01)
        assert "\u2705" in DEFAULT_APPROVAL_EMOJIS
        assert w.deliver(channel_id=10, message_id=20, user_id=30, emoji="\u2705")
        assert (await wait_task) == "\u2705"


# =============================================================================
# install_reaction_handler
# =============================================================================


class TestInstallReactionHandler:
    @pytest.mark.asyncio
    async def test_handler_routes_payload_to_waiter(self):
        client = _FakeClient()
        waiter = ReactionWaiter()
        handler = install_reaction_handler(client, waiter, bot_user_id="bot1")
        assert client.handlers["on_raw_reaction_add"] is handler

        wait_task = asyncio.create_task(waiter.wait(10, 20, 30, timeout=1.0))
        await asyncio.sleep(0.01)
        await handler(_FakePayload(user_id=30, channel_id=10, message_id=20,
                                   emoji="\u2705"))
        assert (await wait_task) == "\u2705"

    @pytest.mark.asyncio
    async def test_handler_skips_bot_self_reactions(self):
        client = _FakeClient()
        waiter = ReactionWaiter()
        handler = install_reaction_handler(client, waiter, bot_user_id="bot1")

        wait_task = asyncio.create_task(waiter.wait(10, 20, 30, timeout=0.1))
        await asyncio.sleep(0.01)
        await handler(_FakePayload(user_id="bot1", channel_id=10,
                                   message_id=20, emoji="\u2705"))
        with pytest.raises(asyncio.TimeoutError):
            await wait_task


# =============================================================================
# Admin operations
# =============================================================================


class TestCreateProjectCategory:
    @pytest.mark.asyncio
    async def test_creates_and_returns_id(self):
        guild = _FakeGuild()
        out = await create_project_category(guild=guild, name="proj-alpha")
        assert out.name == "proj-alpha"
        assert out.category_id == "2000"
        assert len(guild.categories) == 1

    @pytest.mark.asyncio
    async def test_rejects_blank_name(self):
        with pytest.raises(OrchestrationError):
            await create_project_category(guild=_FakeGuild(), name="   ")

    @pytest.mark.asyncio
    async def test_rejects_overlong_name(self):
        with pytest.raises(OrchestrationError, match="exceeds"):
            await create_project_category(guild=_FakeGuild(), name="x" * 101)

    @pytest.mark.asyncio
    async def test_wraps_underlying_error(self):
        guild = _FakeGuild(create_category_should_raise=RuntimeError("api boom"))
        with pytest.raises(OrchestrationError, match="failed to create category"):
            await create_project_category(guild=guild, name="x")


class TestCreateStreamChannel:
    @pytest.mark.asyncio
    async def test_creates_channel_in_category(self):
        cat = _FakeCategory(id=2000, name="proj")
        out = await create_stream_channel(category=cat, name="frontend")
        assert out.name == "frontend"
        assert out.category_id == "2000"
        assert len(cat.channels) == 1

    @pytest.mark.asyncio
    async def test_rejects_invalid_name(self):
        for bad in ("Frontend", "../etc", "front end", "-leading", ""):
            with pytest.raises(OrchestrationError):
                await create_stream_channel(
                    category=_FakeCategory(id=1, name="x"), name=bad
                )

    @pytest.mark.asyncio
    async def test_rejects_non_string_topic(self):
        with pytest.raises(OrchestrationError):
            await create_stream_channel(
                category=_FakeCategory(id=1, name="x"),
                name="frontend",
                topic=123,  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_wraps_underlying_error(self):
        cat = _FakeCategory(
            id=1,
            name="x",
            create_text_channel_should_raise=RuntimeError("forbidden"),
        )
        with pytest.raises(OrchestrationError, match="failed to create channel"):
            await create_stream_channel(category=cat, name="frontend")


class TestArchiveProjectCategory:
    @pytest.mark.asyncio
    async def test_deletes_channels_then_category(self):
        cat = _FakeCategory(id=1, name="x")
        cat.channels = [
            _FakeChannel(id=10, name="frontend"),
            _FakeChannel(id=11, name="backend"),
        ]
        result = await archive_project_category(category=cat, reason="done")
        assert result["category_deleted"] is True
        assert sorted(result["deleted_channels"]) == ["10", "11"]
        assert cat.delete_calls == ["done"]
        for ch in cat.channels:
            assert ch.delete_calls == ["done"]

    @pytest.mark.asyncio
    async def test_filter_keeps_some_channels(self):
        cat = _FakeCategory(id=1, name="x")
        keep_ch = _FakeChannel(id=10, name="frontend")
        del_ch = _FakeChannel(id=11, name="backend")
        cat.channels = [keep_ch, del_ch]

        result = await archive_project_category(
            category=cat, channel_filter=lambda c: c.name != "frontend"
        )
        # Category not deleted because something remained.
        assert result["category_deleted"] is False
        assert result["deleted_channels"] == ["11"]
        assert result["skipped_channels"] == ["10"]
        assert cat.delete_calls == []
        assert keep_ch.delete_calls == []
        assert del_ch.delete_calls == [None]

    @pytest.mark.asyncio
    async def test_wraps_channel_delete_error(self):
        cat = _FakeCategory(id=1, name="x")
        bad = _FakeChannel(id=10, name="frontend",
                           delete_should_raise=RuntimeError("boom"))
        cat.channels = [bad]
        with pytest.raises(OrchestrationError, match="failed to delete channel"):
            await archive_project_category(category=cat)
        # Category not deleted — the failure stops short.
        assert cat.delete_calls == []

    @pytest.mark.asyncio
    async def test_wraps_category_delete_error(self):
        cat = _FakeCategory(id=1, name="x",
                            delete_should_raise=RuntimeError("forbidden"))
        with pytest.raises(OrchestrationError, match="failed to delete category"):
            await archive_project_category(category=cat)


# =============================================================================
# Error-wrapping contract (Codex P3 follow-up)
# =============================================================================
#
# The adapter contract is: this module raises only OrchestrationError.
# A duck-typed backend that returns malformed objects, or a caller that
# supplies a buggy predicate, must NOT leak Python errors past the
# boundary.


class _ShapelessReturn:
    """A 'category' return object missing .id / .name."""


class TestErrorWrapping:
    @pytest.mark.asyncio
    async def test_create_category_wraps_missing_id(self):
        class _Guild:
            async def create_category(self, name, *, overwrites=None, reason=None):
                return _ShapelessReturn()

        with pytest.raises(OrchestrationError, match="failed to create category"):
            await create_project_category(guild=_Guild(), name="x")

    @pytest.mark.asyncio
    async def test_create_channel_wraps_missing_id(self):
        class _Cat:
            id = 7
            async def create_text_channel(self, name, *, topic=None,
                                          overwrites=None, reason=None):
                return _ShapelessReturn()

        with pytest.raises(OrchestrationError, match="failed to create channel"):
            await create_stream_channel(category=_Cat(), name="frontend")

    @pytest.mark.asyncio
    async def test_archive_wraps_filter_exception(self):
        cat = _FakeCategory(id=1, name="x")
        cat.channels = [_FakeChannel(id=10, name="frontend")]

        def boom(_ch):
            raise ValueError("predicate exploded")

        with pytest.raises(OrchestrationError, match="channel_filter raised"):
            await archive_project_category(category=cat, channel_filter=boom)
        # The raise happened before any delete attempt.
        assert cat.channels[0].delete_calls == []
        assert cat.delete_calls == []


# =============================================================================
# Cross-thread delivery + cleanup races (Codex P3 follow-up)
# =============================================================================


class TestCrossThreadDelivery:
    """Reactions arrive on the gateway's discord.py loop, but the waiter
    may be running on a different orchestrator loop. The advertised
    contract is that ``deliver()`` is safe to call from any thread.
    """

    @pytest.mark.asyncio
    async def test_delivery_from_real_thread_resolves_wait(self):
        import threading

        w = ReactionWaiter()
        delivered = threading.Event()

        def deliver_from_thread():
            # Tiny sleep so the wait is registered before we deliver.
            import time as _time
            _time.sleep(0.02)
            ok = w.deliver(channel_id=10, message_id=20, user_id=30,
                           emoji="\u2705")
            if ok:
                delivered.set()

        t = threading.Thread(target=deliver_from_thread)
        t.start()
        try:
            emoji = await w.wait(10, 20, 30, timeout=2.0)
        finally:
            t.join(timeout=2.0)
        assert delivered.is_set(), "deliver() must have accepted the event"
        assert emoji == "\u2705"
        assert w.pending_count() == 0

    @pytest.mark.asyncio
    async def test_deliver_after_timeout_is_a_noop(self):
        """Late deliver (after the waiter has timed out and cleared) must
        not raise and must not resurrect any state.
        """
        w = ReactionWaiter()
        with pytest.raises(asyncio.TimeoutError):
            await w.wait(10, 20, 30, timeout=0.05)
        # Now deliver after the wait is gone — should be a clean False.
        accepted = w.deliver(channel_id=10, message_id=20, user_id=30,
                             emoji="\u2705")
        assert accepted is False
        assert w.pending_count() == 0

    @pytest.mark.asyncio
    async def test_deliver_during_concurrent_cleanup_is_safe(self):
        """Race the in-flight delivery against the wait being cancelled.

        Either outcome is acceptable (resolved-with-emoji OR cancelled),
        but the waiter must not raise InvalidStateError, must not leak
        the pending entry, and must not deadlock.
        """
        w = ReactionWaiter()
        wait_task = asyncio.create_task(w.wait(10, 20, 30, timeout=1.0))
        await asyncio.sleep(0.01)
        # Fire delivery and cancel in the same tick.
        accepted = w.deliver(channel_id=10, message_id=20, user_id=30,
                             emoji="\u2705")
        wait_task.cancel()
        try:
            result = await wait_task
        except asyncio.CancelledError:
            result = None
        # Either path is fine; both must leave the registry clean.
        assert w.pending_count() == 0
        assert accepted in (True, False)
        if result is not None:
            assert result == "\u2705"
