"""Discord adapter used by the orchestrator.

Two implementations:
  - DiscordClient: wraps discord.py against a real bot (lazy-imported so
    importing the package without the messaging extra still works).
  - InMemoryDiscord: a faithful fake used by integration tests; records
    every channel creation and message so the test can assert on them.

Both implement the same minimal surface the orchestrator needs:
    create_channel, send_message, fetch_messages, mention,
    register_message_handler.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional


@dataclass
class DiscordMessage:
    message_id: str
    channel_id: str
    author_id: str
    author_name: str
    content: str
    timestamp: float = field(default_factory=time.time)
    is_bot: bool = False
    mentions: List[str] = field(default_factory=list)


@dataclass
class DiscordChannel:
    channel_id: str
    name: str
    parent_id: Optional[str] = None  # category or parent project channel
    topic: Optional[str] = None


# Sync handler signature: returns nothing. Adapter is responsible for
# calling it on the event loop or a worker thread.
MessageHandler = Callable[[DiscordMessage], None]


class DiscordAdapter(ABC):
    @abstractmethod
    def create_channel(
        self, name: str, *, parent_id: Optional[str] = None, topic: Optional[str] = None
    ) -> DiscordChannel: ...

    @abstractmethod
    def send_message(
        self, channel_id: str, content: str, *, mentions: Optional[List[str]] = None
    ) -> DiscordMessage: ...

    @abstractmethod
    def get_channel(self, channel_id: str) -> Optional[DiscordChannel]: ...

    @abstractmethod
    def fetch_messages(self, channel_id: str, *, limit: int = 50) -> List[DiscordMessage]: ...

    @abstractmethod
    def register_handler(self, handler: MessageHandler) -> None: ...

    def mention(self, user_id: str) -> str:
        return f"<@{user_id}>"


# ── In-memory fake ───────────────────────────────────────────────────────

class InMemoryDiscord(DiscordAdapter):
    """Test double. Stores channels and messages in dicts and broadcasts
    every inbound user message to registered handlers synchronously."""

    def __init__(self):
        self.channels: Dict[str, DiscordChannel] = {}
        self.messages: Dict[str, List[DiscordMessage]] = {}
        self.handlers: List[MessageHandler] = []
        self._lock = threading.RLock()
        self._channel_seq = 0
        self._message_seq = 0

    # ── adapter API ──────────────────────────────────────────────────────
    def create_channel(self, name, *, parent_id=None, topic=None):
        with self._lock:
            self._channel_seq += 1
            ch_id = f"ch_{self._channel_seq:04d}"
            ch = DiscordChannel(channel_id=ch_id, name=name, parent_id=parent_id, topic=topic)
            self.channels[ch_id] = ch
            self.messages.setdefault(ch_id, [])
            return ch

    def send_message(self, channel_id, content, *, mentions=None):
        with self._lock:
            if channel_id not in self.channels:
                raise KeyError(f"unknown channel: {channel_id}")
            self._message_seq += 1
            msg = DiscordMessage(
                message_id=f"msg_{self._message_seq:05d}",
                channel_id=channel_id,
                author_id="bot",
                author_name="MythosBot",
                content=content,
                is_bot=True,
                mentions=list(mentions or []),
            )
            self.messages[channel_id].append(msg)
            return msg

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def fetch_messages(self, channel_id, *, limit=50):
        with self._lock:
            return list(self.messages.get(channel_id, []))[-limit:]

    def register_handler(self, handler):
        self.handlers.append(handler)

    # ── test affordances ─────────────────────────────────────────────────
    def seed_channel(self, name: str, channel_id: Optional[str] = None) -> DiscordChannel:
        with self._lock:
            if channel_id is None:
                self._channel_seq += 1
                channel_id = f"ch_{self._channel_seq:04d}"
            ch = DiscordChannel(channel_id=channel_id, name=name)
            self.channels[channel_id] = ch
            self.messages.setdefault(channel_id, [])
            return ch

    def deliver_user_message(
        self,
        channel_id: str,
        content: str,
        *,
        author_id: str = "user_001",
        author_name: str = "tester",
    ) -> DiscordMessage:
        with self._lock:
            self._message_seq += 1
            msg = DiscordMessage(
                message_id=f"msg_{self._message_seq:05d}",
                channel_id=channel_id,
                author_id=author_id,
                author_name=author_name,
                content=content,
                is_bot=False,
            )
            self.messages.setdefault(channel_id, []).append(msg)
        # Broadcast outside the lock so handlers can call back into us.
        for handler in list(self.handlers):
            handler(msg)
        return msg


# ── Real discord.py adapter ──────────────────────────────────────────────

class DiscordClient(DiscordAdapter):
    """Thin discord.py wrapper. The orchestrator stays sync; this adapter
    bridges by submitting coroutines onto the bot's event loop and waiting
    on the resulting future from the orchestrator's thread.
    """

    def __init__(
        self,
        *,
        bot_token: str,
        guild_id: int,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        try:
            import discord
            from discord.ext import commands
        except ImportError as exc:  # pragma: no cover — documented dep
            raise RuntimeError(
                "discord.py not installed. Install hermes-agent[messaging]."
            ) from exc

        self._discord = discord
        self._bot_token = bot_token
        self._guild_id = int(guild_id)
        self._loop = loop or asyncio.new_event_loop()
        self._handlers: List[MessageHandler] = []
        self._lock = threading.RLock()

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.messages = True
        self._bot = commands.Bot(command_prefix="!", intents=intents)
        self._guild = None
        self._ready = threading.Event()

        @self._bot.event
        async def on_ready():
            self._guild = self._bot.get_guild(self._guild_id)
            self._ready.set()

        @self._bot.event
        async def on_message(msg):
            if msg.author.bot:
                return
            wrapped = DiscordMessage(
                message_id=str(msg.id),
                channel_id=str(msg.channel.id),
                author_id=str(msg.author.id),
                author_name=str(msg.author.display_name),
                content=str(msg.content),
                is_bot=False,
                mentions=[str(u.id) for u in getattr(msg, "mentions", [])],
            )
            for handler in list(self._handlers):
                try:
                    handler(wrapped)
                except Exception:  # pragma: no cover — defensive
                    pass

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> threading.Thread:
        """Run the bot loop in a background thread. Returns the thread."""

        def _runner():
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._bot.start(self._bot_token))

        t = threading.Thread(target=_runner, name="MythosDiscord", daemon=True)
        t.start()
        self._ready.wait(timeout=30)
        return t

    def shutdown(self) -> None:  # pragma: no cover — exercised in real bot only
        async def _close():
            await self._bot.close()
        if self._loop.is_running():
            asyncio.run_coroutine_threadsafe(_close(), self._loop).result(timeout=10)

    # ── adapter API ──────────────────────────────────────────────────────
    def _await(self, coro: Awaitable):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=30)

    def create_channel(self, name, *, parent_id=None, topic=None):
        async def _create():
            guild = self._guild or self._bot.get_guild(self._guild_id)
            parent = guild.get_channel(int(parent_id)) if parent_id else None
            ch = await guild.create_text_channel(name=name, category=parent, topic=topic)
            return ch
        ch = self._await(_create())
        return DiscordChannel(channel_id=str(ch.id), name=ch.name, parent_id=parent_id, topic=topic)

    def send_message(self, channel_id, content, *, mentions=None):
        async def _send():
            channel = self._bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self._bot.fetch_channel(int(channel_id))
            content_with_pings = content
            if mentions:
                ping_text = " ".join(f"<@{u}>" for u in mentions)
                content_with_pings = f"{ping_text}\n{content}"
            sent = await channel.send(content_with_pings)
            return sent
        sent = self._await(_send())
        return DiscordMessage(
            message_id=str(sent.id),
            channel_id=str(channel_id),
            author_id="bot",
            author_name="MythosBot",
            content=content,
            is_bot=True,
            mentions=list(mentions or []),
        )

    def get_channel(self, channel_id):
        async def _get():
            ch = self._bot.get_channel(int(channel_id))
            if ch is None:
                ch = await self._bot.fetch_channel(int(channel_id))
            return ch
        try:
            ch = self._await(_get())
        except Exception:
            return None
        return DiscordChannel(channel_id=str(ch.id), name=ch.name)

    def fetch_messages(self, channel_id, *, limit=50):
        async def _fetch():
            channel = self._bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self._bot.fetch_channel(int(channel_id))
            out = []
            async for m in channel.history(limit=limit):
                out.append(DiscordMessage(
                    message_id=str(m.id),
                    channel_id=str(channel_id),
                    author_id=str(m.author.id),
                    author_name=str(m.author.display_name),
                    content=str(m.content),
                    is_bot=bool(m.author.bot),
                ))
            return list(reversed(out))
        return self._await(_fetch())

    def register_handler(self, handler):
        with self._lock:
            self._handlers.append(handler)
