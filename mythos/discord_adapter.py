"""Discord adapter for mythos.

Defines an abstract `DiscordAdapter` (just the operations mythos needs:
listen for messages, send to channel, create channel under category) plus a
real `DiscordPyAdapter` built on discord.py and a `FakeDiscordAdapter` used
by integration tests.

We deliberately do NOT route through hermes-agent's gateway/platforms/discord.py
in v1 because:
  - hermes' Discord layer is bound to its session lifecycle (one chat = one
    AIAgent session) and would force every Mythos-spawned subprocess to live
    inside a hermes session; that conflicts with our per-channel-per-agent
    confinement model.
  - We want a tight, mockable surface for testing.
The same `discord.py` library that hermes ships is reused — no new deps.
"""

from __future__ import annotations

import abc
import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional


logger = logging.getLogger("mythos.discord")


@dataclass
class IncomingMessage:
    """A message received from Discord."""
    message_id: int
    channel_id: int
    author_id: int
    author_name: str
    content: str
    is_bot: bool = False
    mentions: List[int] = None  # user IDs mentioned

    def __post_init__(self):
        if self.mentions is None:
            self.mentions = []


# Callback signature: async fn(msg: IncomingMessage) -> None
MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class DiscordAdapter(abc.ABC):
    """Operations mythos needs from Discord."""

    bot_user_id: int = 0

    @abc.abstractmethod
    async def start(
        self,
        token: str,
        guild_id: int,
        role_tokens: Optional[Dict[str, str]] = None,
    ) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    def on_message(self, handler: MessageHandler) -> None: ...

    @abc.abstractmethod
    async def send_message(self, channel_id: int, content: str) -> int:
        """Send a message; returns its message ID."""

    async def send_message_as(self, role: str, channel_id: int, content: str) -> int:
        """Send a role-authored message, falling back to the primary bot."""
        return await self.send_message(channel_id, content)

    @abc.abstractmethod
    async def create_text_channel(
        self,
        name: str,
        parent_category_id: Optional[int] = None,
        topic: Optional[str] = None,
    ) -> int:
        """Create a text channel; returns the new channel ID."""

    @abc.abstractmethod
    async def create_category(self, name: str) -> int:
        """Create a category channel; returns the new category ID."""

    @abc.abstractmethod
    async def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> None: ...


# ---------------------------------------------------------------------------
# Real implementation
# ---------------------------------------------------------------------------

class DiscordPyAdapter(DiscordAdapter):
    """Production adapter using discord.py."""

    def __init__(self):
        self._handlers: List[MessageHandler] = []
        self._client = None  # discord.Client
        self._guild_id: int = 0
        self._task: Optional[asyncio.Task] = None
        self._role_clients: Dict[str, object] = {}
        self._role_clients_by_token: Dict[str, object] = {}
        self._role_tasks: List[asyncio.Task] = []

    def on_message(self, handler: MessageHandler) -> None:
        self._handlers.append(handler)

    async def start(
        self,
        token: str,
        guild_id: int,
        role_tokens: Optional[Dict[str, str]] = None,
    ) -> None:
        import discord  # imported lazily so tests don't need it
        self._guild_id = guild_id
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True

        self._client = discord.Client(intents=intents)

        @self._client.event
        async def on_ready():
            self.bot_user_id = self._client.user.id
            logger.info("mythos discord adapter ready as %s", self._client.user)

        @self._client.event
        async def on_message(message):
            if message.guild is None or message.guild.id != self._guild_id:
                return
            inc = IncomingMessage(
                message_id=message.id,
                channel_id=message.channel.id,
                author_id=message.author.id,
                author_name=str(message.author),
                content=message.content or "",
                is_bot=bool(message.author.bot),
                mentions=[u.id for u in message.mentions],
            )
            for h in list(self._handlers):
                try:
                    await h(inc)
                except Exception:
                    logger.exception("mythos message handler crashed")

        self._task = asyncio.create_task(self._client.start(token))
        self._start_role_clients(discord, token, role_tokens or {})

    def _start_role_clients(self, discord, primary_token: str, role_tokens: Dict[str, str]) -> None:
        """Start optional send-only clients keyed by Mythos agent role."""
        for role, role_token in role_tokens.items():
            role_lc = str(role).lower().strip()
            token = (role_token or "").strip()
            if not role_lc or not token or token == primary_token:
                continue
            existing = self._role_clients_by_token.get(token)
            if existing is not None:
                self._role_clients[role_lc] = existing
                continue

            intents = discord.Intents.default()
            intents.guilds = True
            intents.guild_messages = True
            client = discord.Client(intents=intents)

            async def on_ready(role_label=role_lc, role_client=client):
                logger.info(
                    "mythos role bot %s ready as %s",
                    role_label,
                    role_client.user,
                )

            client.event(on_ready)
            self._role_clients[role_lc] = client
            self._role_clients_by_token[token] = client
            self._role_tasks.append(
                asyncio.create_task(client.start(token), name=f"mythos-role-bot:{role_lc}")
            )

    async def stop(self) -> None:
        for client in list(self._role_clients_by_token.values()):
            try:
                await client.close()
            except Exception:
                logger.exception("failed to close mythos role bot client")
        for task in list(self._role_tasks):
            try:
                await task
            except Exception:
                pass
        self._role_clients.clear()
        self._role_clients_by_token.clear()
        self._role_tasks.clear()
        if self._client is not None:
            await self._client.close()
        if self._task is not None:
            try:
                await self._task
            except Exception:
                pass

    async def send_message(self, channel_id: int, content: str) -> int:
        ch = self._client.get_channel(channel_id) or await self._client.fetch_channel(channel_id)
        # Discord caps messages at 2000 chars; chunk if needed.
        chunks = _chunk(content, 1900)
        last_id = 0
        for chunk in chunks:
            msg = await ch.send(chunk)
            last_id = msg.id
        return last_id

    async def send_message_as(self, role: str, channel_id: int, content: str) -> int:
        role_lc = str(role or "").lower().strip()
        client = self._role_clients.get(role_lc)
        if client is None:
            return await self.send_message(channel_id, content)
        try:
            return await self._send_with_client(client, channel_id, content)
        except Exception:
            logger.exception("role bot %s send failed; falling back to primary bot", role_lc)
            return await self.send_message(channel_id, content)

    async def _send_with_client(self, client, channel_id: int, content: str) -> int:
        ch = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        chunks = _chunk(content, 1900)
        last_id = 0
        for chunk in chunks:
            msg = await ch.send(chunk)
            last_id = msg.id
        return last_id

    async def create_text_channel(
        self,
        name: str,
        parent_category_id: Optional[int] = None,
        topic: Optional[str] = None,
    ) -> int:
        guild = self._client.get_guild(self._guild_id)
        if guild is None:
            guild = await self._client.fetch_guild(self._guild_id)
        category = None
        if parent_category_id:
            category = guild.get_channel(parent_category_id)
        ch = await guild.create_text_channel(name=name, category=category, topic=topic or "")
        return ch.id

    async def create_category(self, name: str) -> int:
        guild = self._client.get_guild(self._guild_id)
        if guild is None:
            guild = await self._client.fetch_guild(self._guild_id)
        cat = await guild.create_category(name=name)
        return cat.id

    async def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> None:
        ch = self._client.get_channel(channel_id) or await self._client.fetch_channel(channel_id)
        msg = await ch.fetch_message(message_id)
        await msg.add_reaction(emoji)


def _chunk(content: str, size: int) -> List[str]:
    if not content:
        return [""]
    out = []
    for i in range(0, len(content), size):
        out.append(content[i:i + size])
    return out


# ---------------------------------------------------------------------------
# In-memory fake for tests
# ---------------------------------------------------------------------------

class FakeDiscordAdapter(DiscordAdapter):
    """In-memory Discord for integration tests.

    Tests can `await fake.simulate_user_message(channel_id, user_id, content)`
    to inject a user message; the registered handlers will be invoked.
    Outbound messages are stored on `messages_by_channel`.
    """

    def __init__(self, bot_user_id: int = 9_000_000_000):
        self.bot_user_id = bot_user_id
        self.role_bot_user_ids: Dict[str, int] = {}
        self._handlers: List[MessageHandler] = []
        self.messages_by_channel: Dict[int, List[Dict]] = {}  # channel_id -> list[{id,content,author}]
        self.channels: Dict[int, Dict] = {}  # id -> {name, category_id, topic, type}
        self._next_channel_id = 1_000
        self._next_message_id = 100_000
        self.reactions: Dict[int, List[str]] = {}  # message_id -> [emoji]
        self._started = False

    async def start(
        self,
        token: str,
        guild_id: int,
        role_tokens: Optional[Dict[str, str]] = None,
    ) -> None:
        self._started = True
        self.role_bot_user_ids = {}
        for index, role in enumerate(sorted((role_tokens or {}).keys()), start=1):
            if (role_tokens or {}).get(role):
                self.role_bot_user_ids[str(role).lower()] = self.bot_user_id + index

    async def stop(self) -> None:
        self._started = False

    def on_message(self, handler: MessageHandler) -> None:
        self._handlers.append(handler)

    async def send_message(self, channel_id: int, content: str) -> int:
        self._next_message_id += 1
        mid = self._next_message_id
        self.messages_by_channel.setdefault(channel_id, []).append(
            {
                "id": mid,
                "content": content,
                "author_id": self.bot_user_id,
                "author_role": None,
            }
        )
        return mid

    async def send_message_as(self, role: str, channel_id: int, content: str) -> int:
        role_lc = str(role or "").lower().strip()
        author_id = self.role_bot_user_ids.get(role_lc, self.bot_user_id)
        author_role = role_lc if role_lc in self.role_bot_user_ids else None
        self._next_message_id += 1
        mid = self._next_message_id
        self.messages_by_channel.setdefault(channel_id, []).append(
            {
                "id": mid,
                "content": content,
                "author_id": author_id,
                "author_role": author_role,
            }
        )
        return mid

    async def create_text_channel(
        self,
        name: str,
        parent_category_id: Optional[int] = None,
        topic: Optional[str] = None,
    ) -> int:
        self._next_channel_id += 1
        cid = self._next_channel_id
        self.channels[cid] = {
            "name": name, "category_id": parent_category_id,
            "topic": topic or "", "type": "text",
        }
        self.messages_by_channel[cid] = []
        return cid

    async def create_category(self, name: str) -> int:
        self._next_channel_id += 1
        cid = self._next_channel_id
        self.channels[cid] = {"name": name, "category_id": None, "topic": "", "type": "category"}
        return cid

    async def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> None:
        self.reactions.setdefault(message_id, []).append(emoji)

    # --- test-only helpers ---

    async def simulate_user_message(
        self,
        channel_id: int,
        user_id: int,
        content: str,
        author_name: str = "alice",
        mentions: Optional[List[int]] = None,
    ) -> int:
        self._next_message_id += 1
        mid = self._next_message_id
        # Record it in channel transcript too (so a sent-then-replied conversation reads naturally).
        self.messages_by_channel.setdefault(channel_id, []).append(
            {"id": mid, "content": content, "author_id": user_id, "author_role": None}
        )
        msg = IncomingMessage(
            message_id=mid, channel_id=channel_id, author_id=user_id,
            author_name=author_name, content=content, is_bot=False,
            mentions=list(mentions or []),
        )
        for h in list(self._handlers):
            await h(msg)
        return mid

    def channel_transcript(self, channel_id: int) -> List[str]:
        return [m["content"] for m in self.messages_by_channel.get(channel_id, [])]
