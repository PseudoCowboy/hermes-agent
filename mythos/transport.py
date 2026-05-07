"""Thin Discord transport abstraction used by the Mythos orchestrator.

Production wraps the existing hermes-agent ``DiscordAdapter`` (built on
discord.py); tests use ``FakeDiscordTransport`` which records messages and
fabricates channel IDs.

Decoupling like this is important because the upstream ``DiscordAdapter``
covers a broad surface (DMs, voice, threads, attachments) and we only need
five operations: subscribe to messages, create a channel, send a message,
mention a role, and look up a channel name.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)


@dataclass
class IncomingMessage:
    channel_id: int
    author_id: int
    author_name: str
    content: str
    is_bot: bool = False
    raw: Any = None


MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class DiscordTransport(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def on_message(self, handler: MessageHandler) -> None: ...
    async def send_message(self, channel_id: int, content: str) -> int: ...
    async def create_text_channel(
        self,
        guild_id: int,
        name: str,
        topic: Optional[str] = None,
        parent_channel_id: Optional[int] = None,
    ) -> int: ...
    async def get_channel_name(self, channel_id: int) -> Optional[str]: ...


# ---- Fake transport for tests --------------------------------------------

@dataclass
class _FakeChannel:
    id: int
    name: str
    topic: Optional[str] = None
    parent_id: Optional[int] = None
    messages: List[Dict[str, Any]] = field(default_factory=list)


class FakeDiscordTransport:
    """In-memory transport. Records every send. ``inject_user_message`` lets
    a test simulate a user posting in a channel.
    """

    def __init__(self) -> None:
        self._channels: Dict[int, _FakeChannel] = {}
        self._handlers: List[MessageHandler] = []
        self._next_channel_id = itertools.count(start=1000)
        self._next_message_id = itertools.count(start=10000)

    # -- DiscordTransport protocol ----------------------------------------

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def on_message(self, handler: MessageHandler) -> None:
        self._handlers.append(handler)

    async def send_message(self, channel_id: int, content: str) -> int:
        ch = self._channels.get(channel_id)
        if ch is None:
            ch = _FakeChannel(id=channel_id, name=f"channel-{channel_id}")
            self._channels[channel_id] = ch
        msg_id = next(self._next_message_id)
        ch.messages.append({"id": msg_id, "content": content, "author": "bot"})
        logger.debug("[fake-discord] send to %s: %s", channel_id, content[:80])
        return msg_id

    async def create_text_channel(
        self,
        guild_id: int,
        name: str,
        topic: Optional[str] = None,
        parent_channel_id: Optional[int] = None,
    ) -> int:
        cid = next(self._next_channel_id)
        self._channels[cid] = _FakeChannel(
            id=cid, name=name, topic=topic, parent_id=parent_channel_id
        )
        return cid

    async def get_channel_name(self, channel_id: int) -> Optional[str]:
        ch = self._channels.get(channel_id)
        return ch.name if ch else None

    # -- test helpers -----------------------------------------------------

    def register_existing_channel(self, channel_id: int, name: str) -> None:
        if channel_id not in self._channels:
            self._channels[channel_id] = _FakeChannel(id=channel_id, name=name)

    async def inject_user_message(
        self,
        channel_id: int,
        author_id: int,
        content: str,
        author_name: str = "tester",
    ) -> None:
        if channel_id not in self._channels:
            self._channels[channel_id] = _FakeChannel(
                id=channel_id, name=f"channel-{channel_id}"
            )
        msg = IncomingMessage(
            channel_id=channel_id,
            author_id=author_id,
            author_name=author_name,
            content=content,
        )
        for handler in list(self._handlers):
            await handler(msg)

    def messages_in(self, channel_id: int) -> List[Dict[str, Any]]:
        ch = self._channels.get(channel_id)
        return list(ch.messages) if ch else []

    def channel_names(self) -> List[str]:
        return [c.name for c in self._channels.values()]

    def channel_by_name(self, name: str) -> Optional[_FakeChannel]:
        for ch in self._channels.values():
            if ch.name == name:
                return ch
        return None


# ---- Hermes-backed transport (production) --------------------------------

class HermesDiscordTransport:
    """Thin wrapper around hermes-agent's ``DiscordAdapter``.

    Imported lazily so the module can be loaded in a test environment that
    doesn't have ``discord.py`` installed.
    """

    def __init__(
        self,
        *,
        bot_token: str,
        guild_id: int,
        adapter: Any = None,  # gateway.platforms.discord.DiscordAdapter
    ) -> None:
        self._bot_token = bot_token
        self._guild_id = guild_id
        self._adapter = adapter
        self._handlers: List[MessageHandler] = []
        self._client = None  # type: ignore[assignment]
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._adapter is not None:
            self._client = getattr(self._adapter, "_client", None)
            return
        # Build a minimal discord.py client for standalone use.
        try:
            import discord  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "discord.py is required for HermesDiscordTransport"
            ) from exc

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        self._client = discord.Client(intents=intents)

        @self._client.event
        async def on_message(message):  # type: ignore[no-redef]
            if message.author == self._client.user:
                return
            wrapped = IncomingMessage(
                channel_id=int(message.channel.id),
                author_id=int(message.author.id),
                author_name=str(message.author),
                content=message.content or "",
                is_bot=bool(getattr(message.author, "bot", False)),
                raw=message,
            )
            for handler in list(self._handlers):
                try:
                    await handler(wrapped)
                except Exception:
                    logger.exception("[mythos] handler crashed on message %s", message.id)

        self._task = asyncio.create_task(self._client.start(self._bot_token))

    async def stop(self) -> None:
        if self._client is not None and not self._client.is_closed():
            await self._client.close()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()

    def on_message(self, handler: MessageHandler) -> None:
        self._handlers.append(handler)

    async def send_message(self, channel_id: int, content: str) -> int:
        channel = self._client.get_channel(channel_id) if self._client else None
        if channel is None:
            channel = await self._client.fetch_channel(channel_id)  # type: ignore[union-attr]
        sent = await channel.send(content[:2000])
        return int(sent.id)

    async def create_text_channel(
        self,
        guild_id: int,
        name: str,
        topic: Optional[str] = None,
        parent_channel_id: Optional[int] = None,
    ) -> int:
        guild = self._client.get_guild(guild_id) if self._client else None
        if guild is None:
            guild = await self._client.fetch_guild(guild_id)  # type: ignore[union-attr]
        kwargs: Dict[str, Any] = {"name": name}
        if topic is not None:
            kwargs["topic"] = topic
        if parent_channel_id is not None:
            parent = guild.get_channel(parent_channel_id)
            if parent is not None:
                kwargs["category"] = parent
        new_channel = await guild.create_text_channel(**kwargs)
        return int(new_channel.id)

    async def get_channel_name(self, channel_id: int) -> Optional[str]:
        if self._client is None:
            return None
        channel = self._client.get_channel(channel_id)
        return getattr(channel, "name", None) if channel else None
