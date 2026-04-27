"""``DiscordAdapter`` captures the bot's user id in ``on_ready`` and
late-binds it into ``ReactionWaiter`` (P5).

Mirrors the FakeBot pattern from ``test_discord_connect.py``.
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
    discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, danger=3, green=1, blurple=2, red=3, grey=4, secondary=5)
    discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4)
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    discord_mod.opus = SimpleNamespace(is_loaded=lambda: True)
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

import gateway.platforms.discord as discord_platform  # noqa: E402
from gateway.platforms.discord import DiscordAdapter  # noqa: E402


class FakeTree:
    def __init__(self):
        self.sync = AsyncMock(return_value=[])

    def command(self, *args, **kwargs):
        return lambda fn: fn


class FakeBot:
    """Run on_ready as soon as ``start()`` is awaited.

    ``user`` defaults to a populated SimpleNamespace; tests override
    ``user`` directly to simulate the ``client.user is None`` edge case.
    """

    def __init__(self, *, intents, user=None):
        self.intents = intents
        self.user = (
            user
            if user is not None
            else SimpleNamespace(id=12345, name="HermesBot")
        )
        self._events = {}
        self.tree = FakeTree()

    def event(self, fn):
        self._events[fn.__name__] = fn
        return fn

    async def start(self, token):
        if "on_ready" in self._events:
            await self._events["on_ready"]()

    async def close(self):
        return None


def _patch_intents(monkeypatch):
    intents = SimpleNamespace(
        message_content=False,
        dm_messages=False,
        guild_messages=False,
        members=False,
        voice_states=False,
    )
    monkeypatch.setattr(discord_platform.Intents, "default", lambda: intents)


def _patch_token_lock(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.acquire_scoped_lock",
        lambda scope, identity, metadata=None: (True, None),
    )
    monkeypatch.setattr(
        "gateway.status.release_scoped_lock", lambda scope, identity: None
    )


@pytest.mark.asyncio
async def test_on_ready_captures_bot_user_id_into_adapter_and_waiter(monkeypatch):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    _patch_token_lock(monkeypatch)
    _patch_intents(monkeypatch)
    monkeypatch.setattr(adapter, "_resolve_allowed_usernames", AsyncMock())

    fake_bot = {"instance": None}

    def factory(*, command_prefix, intents):
        bot = FakeBot(intents=intents, user=SimpleNamespace(id=42, name="bot"))
        fake_bot["instance"] = bot
        return bot

    monkeypatch.setattr(discord_platform.commands, "Bot", factory)

    # Pre-condition: nothing captured yet.
    assert adapter.bot_user_id is None
    assert adapter._reaction_waiter.bot_user_id is None

    ok = await adapter.connect()
    assert ok is True

    # Post-condition: on_ready ran during start() and captured the id.
    assert adapter.bot_user_id == 42
    assert adapter._reaction_waiter.bot_user_id == 42

    await adapter.disconnect()
    # disconnect clears the captured id so a stale value doesn't leak
    # into a future reconnect.
    assert adapter.bot_user_id is None
    assert adapter._reaction_waiter.bot_user_id is None


@pytest.mark.asyncio
async def test_on_ready_handles_missing_client_user(monkeypatch):
    """``client.user`` can be ``None`` during fast reconnect races; the
    adapter must not crash and should leave ``bot_user_id`` as None."""
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    _patch_token_lock(monkeypatch)
    _patch_intents(monkeypatch)
    monkeypatch.setattr(adapter, "_resolve_allowed_usernames", AsyncMock())

    def factory(*, command_prefix, intents):
        # Force user=None — emulates the rare reconnect race.
        return FakeBot(intents=intents, user=SimpleNamespace())

    monkeypatch.setattr(discord_platform.commands, "Bot", factory)

    # Replace the FakeBot.user with None after construction so the
    # initial logger.info("Connected as %s", ...) doesn't crash.
    original_factory = discord_platform.commands.Bot

    def factory_with_none(*, command_prefix, intents):
        bot = FakeBot(intents=intents)
        bot.user = None
        return bot

    monkeypatch.setattr(discord_platform.commands, "Bot", factory_with_none)

    ok = await adapter.connect()
    assert ok is True
    assert adapter.bot_user_id is None
    assert adapter._reaction_waiter.bot_user_id is None

    await adapter.disconnect()


def test_reaction_waiter_set_bot_user_id_accepts_none():
    """The setter accepts ``None`` so on_ready can pass through cleanly
    when ``client.user`` is missing."""
    from gateway.platforms.discord_orchestration import ReactionWaiter

    waiter = ReactionWaiter()
    assert waiter.bot_user_id is None
    waiter.set_bot_user_id(99)
    assert waiter.bot_user_id == 99
    waiter.set_bot_user_id(None)
    assert waiter.bot_user_id is None


@pytest.mark.asyncio
async def test_on_ready_reconnect_preserves_last_known_bot_user_id(monkeypatch):
    """On a reconnect where ``client.user`` is transiently None, the
    previously captured id must NOT be clobbered.

    Regression for Codex P5 finding #3.
    """
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    _patch_token_lock(monkeypatch)
    _patch_intents(monkeypatch)
    monkeypatch.setattr(adapter, "_resolve_allowed_usernames", AsyncMock())

    bot_holder = {"user": SimpleNamespace(id=42, name="bot")}

    def factory(*, command_prefix, intents):
        bot = FakeBot(intents=intents, user=bot_holder["user"])
        return bot

    monkeypatch.setattr(discord_platform.commands, "Bot", factory)

    # First connect: id captured.
    ok = await adapter.connect()
    assert ok is True
    assert adapter.bot_user_id == 42
    assert adapter._reaction_waiter.bot_user_id == 42

    # Simulate a fast reconnect race: drive on_ready again with
    # client.user == None.  The captured id MUST stay 42.
    adapter._client.user = None
    on_ready = adapter._client._events["on_ready"]
    await on_ready()

    assert adapter.bot_user_id == 42, "stale bot_user_id was clobbered"
    assert adapter._reaction_waiter.bot_user_id == 42

    await adapter.disconnect()
    # Explicit disconnect still clears.
    assert adapter.bot_user_id is None
