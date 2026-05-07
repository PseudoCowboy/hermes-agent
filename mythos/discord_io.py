"""Thin Discord I/O abstraction used by Mythos.

This module defines a Protocol (``DiscordIO``) that the Mythos
orchestrator depends on. Two implementations:

  * ``RealDiscordIO`` — wraps ``discord.py`` directly (the same library
    hermes-agent's gateway uses). Production path.
  * ``InMemoryDiscordIO`` — a deterministic test double. The integration
    tests use this so they run with no network and no token.

The Protocol surface is deliberately small: post a message, create a
text channel under a category, create a category, fetch the category
of a channel. That is enough to express the full happy path.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Tuple,
)


@dataclass
class IncomingMessage:
    """A normalized Discord message handed to the router."""

    channel_id: int
    user_id: int
    user_name: str
    content: str
    message_id: int
    is_bot: bool = False


# Type alias for the message handler the orchestrator registers.
MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class DiscordIO(Protocol):
    """The minimal Discord surface Mythos needs."""

    async def send(self, channel_id: int, content: str) -> int:
        """Post ``content`` to ``channel_id``; return the message id."""
        ...

    async def create_category(self, guild_id: int, name: str) -> int:
        ...

    async def create_text_channel(
        self, guild_id: int, category_id: int, name: str
    ) -> int:
        ...

    def on_message(self, handler: MessageHandler) -> None:
        """Register a coroutine to be called for every inbound user message."""
        ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# In-memory test double
# --------------------------------------------------------------------------- #

@dataclass
class InMemoryDiscordIO:
    """Deterministic Discord double for tests.

    Thread-safe via the asyncio event loop. ``inject_user_message`` is
    how a test simulates a user typing in a channel; the registered
    handler is awaited inline so ``await`` returns when processing is
    done.
    """

    _next_id: int = 1000
    _categories: Dict[int, str] = field(default_factory=dict)
    _channels: Dict[int, Tuple[Optional[int], str]] = field(default_factory=dict)
    posted: Dict[int, List[Dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    _handler: Optional[MessageHandler] = None

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def send(self, channel_id: int, content: str) -> int:
        # Note: Discord's 2000-char split is exercised in the orchestrator,
        # not the IO layer; the IO layer just records what it was asked to send.
        msg_id = self._new_id()
        self.posted[channel_id].append(
            {"message_id": msg_id, "content": content}
        )
        return msg_id

    async def create_category(self, guild_id: int, name: str) -> int:
        cid = self._new_id()
        self._categories[cid] = name
        return cid

    async def create_text_channel(
        self, guild_id: int, category_id: int, name: str
    ) -> int:
        cid = self._new_id()
        self._channels[cid] = (category_id, name)
        return cid

    def on_message(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def start(self) -> None:  # no-op for tests
        return None

    async def close(self) -> None:  # no-op for tests
        return None

    # ---- Test API ----

    async def inject_user_message(
        self,
        channel_id: int,
        content: str,
        user_id: int = 42,
        user_name: str = "tester",
    ) -> int:
        if self._handler is None:
            raise RuntimeError("No message handler registered")
        msg_id = self._new_id()
        await self._handler(
            IncomingMessage(
                channel_id=channel_id,
                user_id=user_id,
                user_name=user_name,
                content=content,
                message_id=msg_id,
            )
        )
        return msg_id

    def channel_messages(self, channel_id: int) -> List[str]:
        return [m["content"] for m in self.posted.get(channel_id, [])]

    def channel_name(self, channel_id: int) -> Optional[str]:
        info = self._channels.get(channel_id)
        return info[1] if info else None

    def category_name(self, category_id: int) -> Optional[str]:
        return self._categories.get(category_id)


# --------------------------------------------------------------------------- #
# Real adapter (uses discord.py — same lib as hermes-agent's gateway)
# --------------------------------------------------------------------------- #

class RealDiscordIO:
    """Production Discord adapter built directly on ``discord.py``.

    Imports are deferred to ``start()`` so the test suite never touches
    discord.py.
    """

    def __init__(self, token: str) -> None:
        self._token = token
        self._handler: Optional[MessageHandler] = None
        self._client = None  # set in start()
        self._ready = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def on_message(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def start(self) -> None:
        if self._client is not None:
            return
        import discord  # type: ignore  # imported here so tests don't need it
        from discord.ext import commands  # type: ignore

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guild_messages = True
        intents.dm_messages = True
        intents.guilds = True

        self._client = commands.Bot(
            command_prefix="!",
            intents=intents,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True
            ),
        )

        adapter = self

        @self._client.event
        async def on_ready():
            adapter._ready.set()

        @self._client.event
        async def on_message(message):
            try:
                if message.author == self._client.user:
                    return
                if adapter._handler is None:
                    return
                await adapter._handler(
                    IncomingMessage(
                        channel_id=int(message.channel.id),
                        user_id=int(message.author.id),
                        user_name=str(message.author.display_name or message.author.name),
                        content=message.content or "",
                        message_id=int(message.id),
                        is_bot=bool(message.author.bot),
                    )
                )
            except Exception:
                # Never let a handler error kill the websocket loop.
                import logging
                logging.getLogger("mythos.discord").exception(
                    "mythos message handler failed"
                )

        self._task = asyncio.create_task(self._client.start(self._token))
        await self._ready.wait()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
        if self._task is not None:
            try:
                await self._task
            except Exception:
                pass

    async def send(self, channel_id: int, content: str) -> int:
        # Discord caps individual messages at 2000 chars; split safely.
        chunks = _split_2000(content)
        last_id = 0
        ch = self._client.get_channel(channel_id)
        if ch is None:
            ch = await self._client.fetch_channel(channel_id)
        for chunk in chunks:
            msg = await ch.send(chunk)
            last_id = int(msg.id)
        return last_id

    async def create_category(self, guild_id: int, name: str) -> int:
        guild = self._client.get_guild(guild_id) or await self._client.fetch_guild(guild_id)
        cat = await guild.create_category(name=name)
        return int(cat.id)

    async def create_text_channel(
        self, guild_id: int, category_id: int, name: str
    ) -> int:
        import discord  # type: ignore
        guild = self._client.get_guild(guild_id) or await self._client.fetch_guild(guild_id)
        category = guild.get_channel(category_id)
        ch = await guild.create_text_channel(name=name, category=category)
        return int(ch.id)


def _split_2000(text: str) -> List[str]:
    """Split text into ≤2000-char chunks at line boundaries when possible."""
    LIMIT = 1900  # leave a little headroom
    if len(text) <= LIMIT:
        return [text]
    chunks: List[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > LIMIT:
            if cur:
                chunks.append(cur)
                cur = ""
            # Single huge line: hard-cut.
            while len(line) > LIMIT:
                chunks.append(line[:LIMIT])
                line = line[LIMIT:]
        cur += line
    if cur:
        chunks.append(cur)
    return chunks
