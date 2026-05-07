"""Discord bridge — abstracts message-receive, channel-create, message-send.

Two implementations are provided:

* :class:`LiveDiscordBridge` — uses ``discord.py`` (the same library hermes-agent's
  gateway uses) to talk to a real Discord server.
* :class:`InMemoryDiscordBridge` — fully synchronous fake that satisfies the
  same protocol; used by the integration tests so the happy path is covered
  without booting Discord.

The orchestrator only depends on the :class:`DiscordBridge` protocol so the two
implementations are interchangeable.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass
class IncomingMessage:
    channel_id: int
    channel_name: str
    author_id: str
    author_name: str
    content: str
    message_id: str
    is_bot: bool = False


# Handler signature: async fn that takes an incoming message and acts on it.
MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class DiscordBridge(Protocol):
    main_channel_id: int

    async def start(self) -> None: ...
    async def close(self) -> None: ...

    def on_message(self, handler: MessageHandler) -> None: ...

    async def create_channel(self, name: str, *, category: str | None = None) -> int: ...

    async def send(self, channel_id: int, content: str) -> str: ...


# --- In-memory fake ---------------------------------------------------------


@dataclass
class _FakeChannel:
    id: int
    name: str
    category: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)


class InMemoryDiscordBridge:
    """Test/dev bridge that holds channels and messages in memory.

    Tests inject user messages with :meth:`inject_user_message`. Handlers get
    awaited synchronously so each test can assert on the resulting state.
    """

    def __init__(self, *, main_channel_id: int = 1000, main_channel_name: str = "main") -> None:
        self._id_seq = itertools.count(main_channel_id + 1)
        self._msg_seq = itertools.count(1)
        self.main_channel_id = main_channel_id
        self.channels: dict[int, _FakeChannel] = {
            main_channel_id: _FakeChannel(id=main_channel_id, name=main_channel_name),
        }
        self._handlers: list[MessageHandler] = []
        self._channel_create_should_fail: bool = False
        self.bot_user_id: str = "bot:mythos"

    # --- protocol methods ------------------------------------------------

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def on_message(self, handler: MessageHandler) -> None:
        self._handlers.append(handler)

    async def create_channel(self, name: str, *, category: str | None = None) -> int:
        if self._channel_create_should_fail:
            raise RuntimeError("simulated channel create failure")
        cid = next(self._id_seq)
        self.channels[cid] = _FakeChannel(id=cid, name=name, category=category)
        return cid

    async def send(self, channel_id: int, content: str) -> str:
        if channel_id not in self.channels:
            raise KeyError(f"unknown channel {channel_id}")
        mid = f"msg_{next(self._msg_seq)}"
        self.channels[channel_id].messages.append(
            {
                "id": mid,
                "ts": time.time(),
                "author_id": self.bot_user_id,
                "author_name": "MythosBot",
                "content": content,
                "is_bot": True,
            }
        )
        return mid

    # --- test helpers ----------------------------------------------------

    def set_channel_create_failure(self, should_fail: bool) -> None:
        self._channel_create_should_fail = should_fail

    async def inject_user_message(
        self,
        *,
        channel_id: int,
        content: str,
        author_id: str = "user_1",
        author_name: str = "alice",
    ) -> str:
        if channel_id not in self.channels:
            raise KeyError(f"unknown channel {channel_id}")
        mid = f"msg_{next(self._msg_seq)}"
        msg = IncomingMessage(
            channel_id=channel_id,
            channel_name=self.channels[channel_id].name,
            author_id=author_id,
            author_name=author_name,
            content=content,
            message_id=mid,
            is_bot=False,
        )
        self.channels[channel_id].messages.append(
            {
                "id": mid,
                "ts": time.time(),
                "author_id": author_id,
                "author_name": author_name,
                "content": content,
                "is_bot": False,
            }
        )
        for handler in list(self._handlers):
            await handler(msg)
        return mid

    def messages_in(self, channel_id: int) -> list[dict[str, Any]]:
        return list(self.channels[channel_id].messages)

    def channel_named(self, name: str) -> _FakeChannel | None:
        for ch in self.channels.values():
            if ch.name == name:
                return ch
        return None


# --- Live discord.py bridge -------------------------------------------------


class LiveDiscordBridge:
    """Discord.py-backed bridge.

    Used by the production runner. Imports ``discord`` lazily so test
    environments don't require it at import time.
    """

    def __init__(
        self,
        *,
        token: str,
        guild_id: int,
        main_channel_id: int,
        category_name: str = "mythos-projects",
    ) -> None:
        if not token:
            raise ValueError("Discord bot token is required")
        if not main_channel_id:
            raise ValueError("Main channel id is required")
        self._token = token
        self._guild_id = guild_id
        self.main_channel_id = main_channel_id
        self._category_name = category_name
        self._handlers: list[MessageHandler] = []
        self._client = None  # type: ignore[assignment]
        self._ready_event = asyncio.Event()

    async def start(self) -> None:
        import discord  # type: ignore

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        client = discord.Client(intents=intents)
        self._client = client

        @client.event
        async def on_ready() -> None:  # noqa: D401 - discord callback
            logger.info("Mythos bridge connected as %s", client.user)
            self._ready_event.set()

        @client.event
        async def on_message(msg: Any) -> None:  # noqa: D401 - discord callback
            if msg.author.bot:
                return
            inc = IncomingMessage(
                channel_id=msg.channel.id,
                channel_name=getattr(msg.channel, "name", ""),
                author_id=str(msg.author.id),
                author_name=str(msg.author),
                content=msg.content or "",
                message_id=str(msg.id),
                is_bot=False,
            )
            for handler in list(self._handlers):
                try:
                    await handler(inc)
                except Exception:  # pragma: no cover - defensive
                    logger.exception("Mythos handler failed")

        # Run the client in a background task; caller drives the loop.
        self._task = asyncio.create_task(client.start(self._token))
        await self._ready_event.wait()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    def on_message(self, handler: MessageHandler) -> None:
        self._handlers.append(handler)

    async def create_channel(self, name: str, *, category: str | None = None) -> int:
        import discord  # type: ignore

        if self._client is None:
            raise RuntimeError("Discord bridge not started")
        guild = self._client.get_guild(self._guild_id)
        if guild is None:
            raise RuntimeError(f"Guild {self._guild_id} not visible to bot")
        cat_obj = None
        target_cat = category or self._category_name
        if target_cat:
            cat_obj = discord.utils.get(guild.categories, name=target_cat)
            if cat_obj is None:
                cat_obj = await guild.create_category(target_cat)
        channel = await guild.create_text_channel(name, category=cat_obj)
        return channel.id

    async def send(self, channel_id: int, content: str) -> str:
        if self._client is None:
            raise RuntimeError("Discord bridge not started")
        channel = self._client.get_channel(channel_id)
        if channel is None:
            channel = await self._client.fetch_channel(channel_id)
        msg = await channel.send(content)
        return str(msg.id)
