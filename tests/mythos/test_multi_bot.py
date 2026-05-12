"""Tests for multi-bot Discord identity (per-role bot tokens)."""

from __future__ import annotations

import json
import os
from unittest import mock

import pytest

from mythos.config import _parse_bot_tokens, load_config
from mythos.discord_io import InMemoryDiscordIO, MultiBotDiscordIO
from mythos.roles import Role


def test_parse_bot_tokens_dict():
    raw = json.dumps({
        "hermes": "tok-A",
        "prometheus": "tok-P",
        "argus": "tok-R",
        "unknown_role": "ignored",
        "apollo": "",  # blank → skipped
    })
    out = _parse_bot_tokens(raw, fallback_token="")
    assert out == {
        Role.HERMES: "tok-A",
        Role.PROMETHEUS: "tok-P",
        Role.ARGUS: "tok-R",
    }


def test_parse_bot_tokens_falls_back_to_legacy_for_hermes():
    raw = json.dumps({"prometheus": "tok-P"})
    out = _parse_bot_tokens(raw, fallback_token="legacy-token")
    assert out[Role.HERMES] == "legacy-token"
    assert out[Role.PROMETHEUS] == "tok-P"


def test_parse_bot_tokens_invalid_json_returns_empty():
    assert _parse_bot_tokens("not-json", fallback_token="") == {}
    assert _parse_bot_tokens("[1,2]", fallback_token="") == {}


def test_load_config_reads_multibot_env(tmp_path):
    env = {
        "DISCORD_BOT_TOKEN": "legacy",
        "DISCORD_GUILD_ID": "1",
        "MYTHOS_MAIN_CHANNEL_ID": "2",
        "MYTHOS_BOT_TOKENS": json.dumps(
            {"hermes": "A", "prometheus": "P"}
        ),
    }
    with mock.patch.dict(os.environ, env, clear=False):
        cfg = load_config(path=tmp_path / "missing.yaml")
    assert cfg.bot_tokens == {Role.HERMES: "A", Role.PROMETHEUS: "P"}


def test_load_config_no_multibot_env_leaves_bot_tokens_empty(tmp_path):
    env = {
        "DISCORD_BOT_TOKEN": "legacy",
        "DISCORD_GUILD_ID": "1",
        "MYTHOS_MAIN_CHANNEL_ID": "2",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = load_config(path=tmp_path / "missing.yaml")
    assert cfg.bot_tokens == {}
    assert cfg.discord_token == "legacy"


def test_multibot_io_requires_hermes():
    with pytest.raises(ValueError):
        MultiBotDiscordIO(tokens={Role.PROMETHEUS: "p"})


# --- Routing test --------------------------------------------------------- #
# We don't want to spin up real discord.py clients. Stub RealDiscordIO at
# the module level for the routing test.

class _StubClient:
    """Records sends so we can assert which underlying client was used."""

    def __init__(self, token, *, listen=True):
        self.token = token
        self.listen = listen
        self.sends = []  # list of (channel_id, content, role)
        self.handler = None
        self.started = False
        self.closed = False
        self.uses_role_identity = False

    def on_message(self, handler):
        self.handler = handler

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def send(self, channel_id, content, role=None):
        self.sends.append((channel_id, content, role))
        return 9999


@pytest.mark.asyncio
async def test_multibot_io_routes_by_role(monkeypatch):
    from mythos import discord_io as dio
    monkeypatch.setattr(dio, "RealDiscordIO", _StubClient)

    io = dio.MultiBotDiscordIO(tokens={
        Role.HERMES: "A",
        Role.PROMETHEUS: "P",
        Role.ARGUS: "R",
    })

    await io.send(123, "hello from prometheus", role=Role.PROMETHEUS)
    await io.send(123, "hello from hermes", role=Role.HERMES)
    # Apollo has no token; should route to Hermes.
    await io.send(123, "fallback", role=Role.APOLLO)
    # Role omitted entirely also routes to Hermes.
    await io.send(123, "no role", role=None)

    p_client = io._clients[Role.PROMETHEUS]
    a_client = io._clients[Role.HERMES]
    r_client = io._clients[Role.ARGUS]

    assert [s[1] for s in p_client.sends] == ["hello from prometheus"]
    assert [s[1] for s in a_client.sends] == [
        "hello from hermes", "fallback", "no role"
    ]
    assert r_client.sends == []
    assert io.uses_role_identity is True


@pytest.mark.asyncio
async def test_multibot_io_only_hermes_listens(monkeypatch):
    from mythos import discord_io as dio
    monkeypatch.setattr(dio, "RealDiscordIO", _StubClient)

    io = dio.MultiBotDiscordIO(tokens={
        Role.HERMES: "A",
        Role.PROMETHEUS: "P",
    })
    assert io._clients[Role.HERMES].listen is True
    assert io._clients[Role.PROMETHEUS].listen is False

    handler_called = []

    async def handler(msg):
        handler_called.append(msg)

    io.on_message(handler)
    assert io._clients[Role.HERMES].handler is handler
    assert io._clients[Role.PROMETHEUS].handler is None


@pytest.mark.asyncio
async def test_inmemory_io_records_role():
    io = InMemoryDiscordIO()
    assert io.uses_role_identity is False
    await io.send(1, "hi", role=Role.PROMETHEUS)
    posted = io.posted[1]
    assert posted[0]["content"] == "hi"
    assert posted[0]["role"] == Role.PROMETHEUS
