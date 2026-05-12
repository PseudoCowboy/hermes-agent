"""Tests for Discord role-bot outbound routing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

from gateway.config import DiscordRoleBotConfig, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.discord import DiscordAdapter


@dataclass
class _FakeMessage:
    id: int
    content: str


@dataclass
class _FakeChannel:
    id: int
    sent: List[str] = field(default_factory=list)
    fail: bool = False

    async def send(self, content=None, reference=None):
        if self.fail:
            raise RuntimeError("send failed")
        text = content if content is not None else ""
        self.sent.append(text)
        return _FakeMessage(id=1000 + len(self.sent), content=text)

    async def fetch_message(self, message_id: int):
        return _FakeMessage(id=message_id, content="old")


class _FakeClient:
    def __init__(self, channels: Dict[int, _FakeChannel]):
        self.channels = channels

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        return self.channels.get(channel_id)


def _adapter() -> DiscordAdapter:
    return DiscordAdapter(PlatformConfig(enabled=True, token="primary-token"))


@pytest.mark.asyncio
async def test_send_for_role_uses_configured_role_client():
    adapter = _adapter()
    primary_channel = _FakeChannel(id=10)
    role_channel = _FakeChannel(id=10)
    adapter._client = _FakeClient({10: primary_channel})
    adapter._role_bot_clients["frontend"] = _FakeClient({10: role_channel})

    result = await adapter.send_for_role("apollo", "10", "hello from frontend")

    assert isinstance(result, SendResult)
    assert result.success is True
    assert role_channel.sent == ["hello from frontend"]
    assert primary_channel.sent == []


@pytest.mark.asyncio
async def test_send_for_role_falls_back_to_primary_when_role_missing():
    adapter = _adapter()
    primary_channel = _FakeChannel(id=10)
    adapter._client = _FakeClient({10: primary_channel})

    result = await adapter.send_for_role("backend", "10", "hello from backend")

    assert result.success is True
    assert primary_channel.sent == ["hello from backend"]


@pytest.mark.asyncio
async def test_send_for_role_falls_back_to_primary_when_role_send_fails():
    adapter = _adapter()
    primary_channel = _FakeChannel(id=10)
    role_channel = _FakeChannel(id=10, fail=True)
    adapter._client = _FakeClient({10: primary_channel})
    adapter._role_bot_clients["frontend"] = _FakeClient({10: role_channel})

    result = await adapter.send_for_role("frontend", "10", "fallback text")

    assert result.success is True
    assert role_channel.sent == []
    assert primary_channel.sent == ["fallback text"]


def test_configured_role_bot_tokens_normalizes_aliases(monkeypatch):
    monkeypatch.setenv("ATLAS_TOKEN", "backend-env-token")
    adapter = DiscordAdapter(
        PlatformConfig(
            enabled=True,
            token="primary-token",
            role_bots={
                "Apollo": DiscordRoleBotConfig(token="frontend-token"),
                "atlas": DiscordRoleBotConfig(token_env="ATLAS_TOKEN"),
            },
        )
    )

    assert adapter._configured_role_bot_tokens() == {
        "frontend": "frontend-token",
        "backend": "backend-env-token",
    }


@pytest.mark.asyncio
async def test_connect_role_bots_reuses_one_client_for_shared_token(monkeypatch):
    created = []

    class _FakeIntents:
        guild_messages = False

    class _FakeClient:
        def __init__(self, intents=None):
            self.intents = intents
            self.user = "role-bot"
            self.closed = False
            self.on_ready = None
            created.append(self)

        def event(self, fn):
            setattr(self, fn.__name__, fn)
            return fn

        async def start(self, token):
            await self.on_ready()
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    monkeypatch.setattr("gateway.platforms.discord.DISCORD_AVAILABLE", True)
    monkeypatch.setattr(
        "gateway.platforms.discord.Intents",
        SimpleNamespace(default=lambda: _FakeIntents()),
    )
    monkeypatch.setattr(
        "gateway.platforms.discord.discord",
        SimpleNamespace(Client=lambda intents=None: _FakeClient(intents=intents)),
    )
    monkeypatch.setattr(
        "gateway.status.acquire_scoped_lock",
        lambda *args, **kwargs: (True, None),
    )
    released = []
    monkeypatch.setattr(
        "gateway.status.release_scoped_lock",
        lambda namespace, token: released.append((namespace, token)),
    )

    adapter = DiscordAdapter(
        PlatformConfig(
            enabled=True,
            token="primary-token",
            role_bots={
                "frontend": DiscordRoleBotConfig(token="shared-token"),
                "backend": DiscordRoleBotConfig(token="shared-token"),
            },
        )
    )

    await adapter._connect_role_bots()

    assert len(created) == 1
    assert adapter._role_bot_clients["frontend"] is created[0]
    assert adapter._role_bot_clients["backend"] is created[0]

    await adapter._disconnect_role_bots()
    assert created[0].closed is True
    assert released == [("discord-bot-token", "shared-token")]
