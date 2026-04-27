"""Tests for tools.discord_orchestration_tools.

These exercise the tool wrappers via the real ``ToolRegistry.dispatch``
path, with a fake DiscordAdapter injected via
``set_active_adapter()`` so no live Discord connection is required.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest

# Importing the tools module triggers registry.register() side effects.
from tools import discord_orchestration_tools  # noqa: F401
from tools.registry import registry

from gateway.platforms.discord_orchestration import (
    OrchestrationError,
    ReactionWaiter,
    clear_active_adapter,
    set_active_adapter,
)


# =============================================================================
# Fakes
# =============================================================================


@dataclass
class _FakeMessage:
    id: int
    content: str = ""
    reactions_added: List[str] = field(default_factory=list)

    async def add_reaction(self, emoji: str) -> None:
        self.reactions_added.append(emoji)


@dataclass
class _FakeChannel:
    id: int
    name: str = "frontend"
    sent: List[str] = field(default_factory=list)
    delete_calls: list = field(default_factory=list)
    _msg_seq: int = 0

    async def send(self, content: str) -> _FakeMessage:
        self.sent.append(content)
        self._msg_seq += 1
        return _FakeMessage(id=900000 + self._msg_seq, content=content)

    async def fetch_message(self, message_id: int) -> _FakeMessage:
        return _FakeMessage(id=message_id)

    async def delete(self, *, reason: Optional[str] = None) -> None:
        self.delete_calls.append(reason)


@dataclass
class _FakeCategory:
    id: int
    name: str
    channels: list = field(default_factory=list)
    delete_calls: list = field(default_factory=list)

    async def create_text_channel(
        self, name: str, *, topic=None, overwrites=None, reason=None
    ) -> _FakeChannel:
        ch = _FakeChannel(id=10000 + len(self.channels), name=name)
        self.channels.append(ch)
        return ch

    async def delete(self, *, reason=None) -> None:
        self.delete_calls.append(reason)


@dataclass
class _FakeGuild:
    id: int = 7777
    categories: list = field(default_factory=list)
    extra_channels: dict = field(default_factory=dict)

    async def create_category(self, name: str, *, overwrites=None, reason=None) -> _FakeCategory:
        cat = _FakeCategory(id=2000 + len(self.categories), name=name)
        self.categories.append(cat)
        return cat

    def get_channel(self, cid: int):
        for cat in self.categories:
            if cat.id == cid:
                return cat
            for ch in cat.channels:
                if ch.id == cid:
                    return ch
        return self.extra_channels.get(cid)


class _FakeClient:
    def __init__(self, guild: _FakeGuild) -> None:
        self.guilds = [guild]
        self._guild_by_id = {guild.id: guild}

    def get_guild(self, gid: int):
        return self._guild_by_id.get(gid)

    def get_channel(self, cid: int):
        # Defer to per-guild lookup for simplicity in fakes.
        for g in self.guilds:
            ch = g.get_channel(cid)
            if ch is not None:
                return ch
        return None


class _FakeAdapter:
    def __init__(self) -> None:
        self.guild = _FakeGuild()
        self._client = _FakeClient(self.guild)
        self._reaction_waiter = ReactionWaiter()


@pytest.fixture()
def adapter():
    fake = _FakeAdapter()
    set_active_adapter(fake)
    try:
        yield fake
    finally:
        clear_active_adapter()


# =============================================================================
# Helpers
# =============================================================================


def _dispatch(name: str, args: dict) -> dict:
    out = registry.dispatch(name, args)
    return json.loads(out)


# =============================================================================
# Active-adapter singleton
# =============================================================================


class TestSingletonLifecycle:
    def test_get_without_set_raises(self):
        clear_active_adapter()
        from gateway.platforms.discord_orchestration import get_active_adapter
        with pytest.raises(OrchestrationError, match="no active discord adapter"):
            get_active_adapter()

    def test_set_then_get_roundtrip(self):
        sentinel = object()
        set_active_adapter(sentinel)
        try:
            from gateway.platforms.discord_orchestration import get_active_adapter
            assert get_active_adapter() is sentinel
        finally:
            clear_active_adapter()

    def test_clear_idempotent(self):
        clear_active_adapter()
        clear_active_adapter()  # second call is fine

    def test_handler_returns_error_when_adapter_missing(self):
        clear_active_adapter()
        out = _dispatch(
            "discord_create_project_category",
            {"guild_id": "7777", "name": "x"},
        )
        assert "error" in out
        assert "no active discord adapter" in out["error"]


# =============================================================================
# discord_create_project_category
# =============================================================================


class TestCreateProjectCategory:
    def test_happy_path(self, adapter):
        out = _dispatch(
            "discord_create_project_category",
            {"guild_id": "7777", "name": "proj-alpha"},
        )
        assert "error" not in out, out
        assert out["name"] == "proj-alpha"
        assert int(out["category_id"]) == adapter.guild.categories[0].id

    def test_accepts_int_guild_id(self, adapter):
        out = _dispatch(
            "discord_create_project_category",
            {"guild_id": 7777, "name": "p"},
        )
        assert "error" not in out

    def test_unknown_guild(self, adapter):
        out = _dispatch(
            "discord_create_project_category",
            {"guild_id": "9999999", "name": "p"},
        )
        assert "error" in out
        assert "guild" in out["error"]

    def test_invalid_guild_id(self, adapter):
        out = _dispatch(
            "discord_create_project_category",
            {"guild_id": "not-a-number", "name": "p"},
        )
        assert "error" in out
        assert "numeric" in out["error"]

    def test_blank_name_propagates_validation(self, adapter):
        out = _dispatch(
            "discord_create_project_category",
            {"guild_id": "7777", "name": "   "},
        )
        assert "error" in out


# =============================================================================
# discord_create_stream_channel
# =============================================================================


class TestCreateStreamChannel:
    def test_happy_path(self, adapter):
        # First create a category to attach the channel to.
        _dispatch(
            "discord_create_project_category",
            {"guild_id": "7777", "name": "proj"},
        )
        cat_id = str(adapter.guild.categories[0].id)
        out = _dispatch(
            "discord_create_stream_channel",
            {"category_id": cat_id, "name": "frontend"},
        )
        assert "error" not in out, out
        assert out["category_id"] == cat_id
        assert out["name"] == "frontend"

    def test_unknown_category(self, adapter):
        out = _dispatch(
            "discord_create_stream_channel",
            {"category_id": "99999", "name": "frontend"},
        )
        assert "error" in out
        assert "channel" in out["error"]

    def test_invalid_channel_name(self, adapter):
        _dispatch(
            "discord_create_project_category",
            {"guild_id": "7777", "name": "proj"},
        )
        cat_id = str(adapter.guild.categories[0].id)
        out = _dispatch(
            "discord_create_stream_channel",
            {"category_id": cat_id, "name": "Frontend"},  # uppercase rejected
        )
        assert "error" in out


# =============================================================================
# discord_archive_project_category
# =============================================================================


class TestArchiveProjectCategory:
    def test_archive_with_no_keep(self, adapter):
        cat = _FakeCategory(id=4242, name="proj")
        cat.channels = [_FakeChannel(id=10, name="frontend")]
        adapter.guild.categories.append(cat)

        out = _dispatch(
            "discord_archive_project_category",
            {"category_id": "4242"},
        )
        assert "error" not in out, out
        assert out["category_deleted"] is True
        assert out["deleted_channels"] == ["10"]

    def test_archive_keeps_channels(self, adapter):
        cat = _FakeCategory(id=4243, name="proj")
        cat.channels = [
            _FakeChannel(id=11, name="frontend"),
            _FakeChannel(id=12, name="backend"),
        ]
        adapter.guild.categories.append(cat)

        out = _dispatch(
            "discord_archive_project_category",
            {"category_id": "4243", "keep_channel_ids": ["11"]},
        )
        assert "error" not in out
        assert out["category_deleted"] is False
        assert out["deleted_channels"] == ["12"]
        assert out["skipped_channels"] == ["11"]

    def test_keep_must_be_list(self, adapter):
        cat = _FakeCategory(id=4244, name="proj")
        adapter.guild.categories.append(cat)
        out = _dispatch(
            "discord_archive_project_category",
            {"category_id": "4244", "keep_channel_ids": "11"},
        )
        assert "error" in out
        assert "array" in out["error"]


# =============================================================================
# discord_post_message
# =============================================================================


class TestPostMessage:
    def test_happy_path(self, adapter):
        ch = _FakeChannel(id=5555, name="general")
        adapter.guild.extra_channels[5555] = ch
        out = _dispatch(
            "discord_post_message",
            {"channel_id": "5555", "content": "hello"},
        )
        assert "error" not in out, out
        assert ch.sent == ["hello"]
        assert int(out["message_id"]) >= 900000

    def test_blank_content_rejected(self, adapter):
        ch = _FakeChannel(id=5556)
        adapter.guild.extra_channels[5556] = ch
        out = _dispatch(
            "discord_post_message",
            {"channel_id": "5556", "content": ""},
        )
        assert "error" in out


# =============================================================================
# discord_react_to_message
# =============================================================================


class TestReactToMessage:
    def test_happy_path(self, adapter):
        ch = _FakeChannel(id=6000)
        adapter.guild.extra_channels[6000] = ch
        out = _dispatch(
            "discord_react_to_message",
            {"channel_id": "6000", "message_id": "42", "emoji": "\u2705"},
        )
        assert "error" not in out, out
        assert out["emoji"] == "\u2705"
        assert out["message_id"] == "42"

    def test_invalid_emoji(self, adapter):
        ch = _FakeChannel(id=6001)
        adapter.guild.extra_channels[6001] = ch
        out = _dispatch(
            "discord_react_to_message",
            {"channel_id": "6001", "message_id": "42", "emoji": ""},
        )
        assert "error" in out


# =============================================================================
# discord_wait_for_reaction
# =============================================================================


class TestWaitForReaction:
    def test_resolves_when_reaction_delivered(self, adapter):
        # Pre-arm the waiter on a thread, then dispatch the tool which
        # blocks until the delivery happens.
        import threading
        import time as _time

        def deliver_after_delay():
            _time.sleep(0.05)
            adapter._reaction_waiter.deliver(
                channel_id=10, message_id=20, user_id=30, emoji="\u2705"
            )

        t = threading.Thread(target=deliver_after_delay)
        t.start()
        try:
            out = _dispatch(
                "discord_wait_for_reaction",
                {"channel_id": "10", "message_id": "20", "user_id": "30",
                 "timeout_s": 2.0},
            )
        finally:
            t.join(timeout=2.0)
        assert "error" not in out, out
        assert out["timed_out"] is False
        assert out["emoji"] == "\u2705"

    def test_timeout_returns_timed_out(self, adapter):
        out = _dispatch(
            "discord_wait_for_reaction",
            {"channel_id": "10", "message_id": "20", "user_id": "30",
             "timeout_s": 0.05},
        )
        assert out == {"timed_out": True}

    def test_invalid_timeout(self, adapter):
        out = _dispatch(
            "discord_wait_for_reaction",
            {"channel_id": "10", "message_id": "20", "user_id": "30",
             "timeout_s": "soon"},
        )
        assert "error" in out
        assert "timeout_s" in out["error"]

    def test_allowed_emojis_validated(self, adapter):
        out = _dispatch(
            "discord_wait_for_reaction",
            {"channel_id": "10", "message_id": "20", "user_id": "30",
             "allowed_emojis": "\u2705"},  # not a list
        )
        assert "error" in out
