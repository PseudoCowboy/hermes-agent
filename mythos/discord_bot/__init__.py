"""Discord transport abstraction.

The orchestrator never imports discord.py directly. It talks to a
`DiscordTransport` interface with two implementations:

  - `RealDiscordTransport`: wraps discord.py's `commands.Bot`. Used in
    production. Requires the `messaging` extra.
  - `FakeDiscordTransport`: in-memory simulation that records all messages
    and channel-creation calls. Used by integration tests and dry runs.

This keeps Discord I/O out of the orchestrator and makes the happy path
testable without a real Discord server.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Protocol


@dataclass
class IncomingMessage:
    channel_id: str
    user_id: str
    user_name: str
    content: str
    message_id: str
    is_bot: bool = False


@dataclass
class OutgoingMessage:
    channel_id: str
    content: str
    message_id: str = ""
    mentions_role: str = ""        # mythos role being pinged (logical)


MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class DiscordTransport(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    def set_message_handler(self, handler: MessageHandler) -> None: ...

    async def create_project_channel(
        self, project_id: str, channel_name: str, parent_category_id: str = ""
    ) -> str: ...

    async def create_subchannel(
        self, project_id: str, parent_channel_id: str, channel_name: str
    ) -> str: ...

    async def send(self, channel_id: str, content: str) -> str: ...


# ---------------------------------------------------------------------------
# Fake transport for tests / dry runs
# ---------------------------------------------------------------------------


class FakeDiscordTransport:
    """In-memory Discord transport.

    Tests drive it by calling `simulate_user_message`. They inspect
    `messages_by_channel` and `channels` to assert behavior.
    """

    def __init__(self, main_channel_id: str = "main"):
        self.main_channel_id = main_channel_id
        self._handler: Optional[MessageHandler] = None
        self._channel_counter = 0
        self._message_counter = 0
        self._lock = threading.Lock()
        self.channels: Dict[str, Dict[str, str]] = {
            main_channel_id: {"name": "main", "type": "main", "parent": ""},
        }
        self.messages_by_channel: Dict[str, List[OutgoingMessage]] = {}
        self.user_messages: List[IncomingMessage] = []
        self.started = False

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    def _new_channel_id(self, prefix: str) -> str:
        with self._lock:
            self._channel_counter += 1
            return f"chan_{prefix}_{self._channel_counter}"

    def _new_message_id(self) -> str:
        with self._lock:
            self._message_counter += 1
            return f"msg_{self._message_counter}"

    async def create_project_channel(
        self, project_id: str, channel_name: str, parent_category_id: str = ""
    ) -> str:
        cid = self._new_channel_id("proj")
        self.channels[cid] = {
            "name": channel_name, "type": "project",
            "parent": parent_category_id, "project_id": project_id,
        }
        self.messages_by_channel.setdefault(cid, [])
        return cid

    async def create_subchannel(
        self, project_id: str, parent_channel_id: str, channel_name: str
    ) -> str:
        cid = self._new_channel_id(channel_name)
        self.channels[cid] = {
            "name": channel_name, "type": "sub",
            "parent": parent_channel_id, "project_id": project_id,
        }
        self.messages_by_channel.setdefault(cid, [])
        return cid

    async def send(self, channel_id: str, content: str) -> str:
        if channel_id not in self.channels:
            raise ValueError(f"unknown channel: {channel_id}")
        mid = self._new_message_id()
        msg = OutgoingMessage(channel_id=channel_id, content=content, message_id=mid)
        self.messages_by_channel.setdefault(channel_id, []).append(msg)
        return mid

    # ---- test helpers -----------------------------------------------------

    async def simulate_user_message(
        self,
        channel_id: str,
        user_id: str,
        content: str,
        user_name: str = "tester",
    ) -> str:
        mid = self._new_message_id()
        ev = IncomingMessage(
            channel_id=channel_id, user_id=user_id, user_name=user_name,
            content=content, message_id=mid,
        )
        self.user_messages.append(ev)
        if self._handler is None:
            raise RuntimeError("no handler installed")
        await self._handler(ev)
        return mid

    def all_messages(self) -> List[OutgoingMessage]:
        out: List[OutgoingMessage] = []
        for msgs in self.messages_by_channel.values():
            out.extend(msgs)
        return out


# ---------------------------------------------------------------------------
# Real Discord transport (discord.py wrapper)
# ---------------------------------------------------------------------------


class RealDiscordTransport:
    """Wraps discord.py for production. Imported lazily so tests don't
    require discord.py to be installed.
    """

    def __init__(self, bot_token: str, guild_id: str, main_channel_id: str):
        if not bot_token:
            raise ValueError("DISCORD_BOT_TOKEN must be configured")
        try:
            import discord  # noqa: F401
            from discord.ext import commands  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "discord.py is required for RealDiscordTransport. "
                "Install with: pip install 'discord.py>=2.7.1,<3'"
            ) from exc

        import discord
        from discord.ext import commands

        self._discord = discord
        self.bot_token = bot_token
        self.guild_id = int(guild_id) if guild_id else 0
        self.main_channel_id = int(main_channel_id) if main_channel_id else 0
        self._handler: Optional[MessageHandler] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.messages = True
        self.bot = commands.Bot(command_prefix="!mythos ", intents=intents)

        @self.bot.event
        async def on_message(message):  # noqa: ANN001
            if message.author.bot:
                return
            if not self._handler:
                return
            ev = IncomingMessage(
                channel_id=str(message.channel.id),
                user_id=str(message.author.id),
                user_name=str(message.author.display_name),
                content=str(message.content),
                message_id=str(message.id),
                is_bot=False,
            )
            await self._handler(ev)

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.bot.start(self.bot_token)

    async def stop(self) -> None:
        await self.bot.close()

    def _guild(self):
        return self.bot.get_guild(self.guild_id) if self.guild_id else None

    async def create_project_channel(
        self, project_id: str, channel_name: str, parent_category_id: str = ""
    ) -> str:
        guild = self._guild()
        if guild is None:
            raise RuntimeError(f"Guild {self.guild_id} not found in cache")
        category = None
        if parent_category_id:
            category = guild.get_channel(int(parent_category_id))
        ch = await guild.create_text_channel(
            name=channel_name, category=category,
            topic=f"Mythos project {project_id}",
        )
        return str(ch.id)

    async def create_subchannel(
        self, project_id: str, parent_channel_id: str, channel_name: str
    ) -> str:
        guild = self._guild()
        if guild is None:
            raise RuntimeError(f"Guild {self.guild_id} not found in cache")
        # Group sub-channels under the same category as the project channel
        parent = guild.get_channel(int(parent_channel_id))
        category = parent.category if parent is not None else None
        ch = await guild.create_text_channel(
            name=channel_name, category=category,
            topic=f"Mythos project {project_id} – {channel_name}",
        )
        return str(ch.id)

    async def send(self, channel_id: str, content: str) -> str:
        ch = self.bot.get_channel(int(channel_id))
        if ch is None:
            ch = await self.bot.fetch_channel(int(channel_id))
        # Discord enforces a 2000-char limit per message; chunk politely.
        chunks = _chunk_for_discord(content)
        last_id = ""
        for chunk in chunks:
            sent = await ch.send(chunk)
            last_id = str(sent.id)
        return last_id


def _chunk_for_discord(text: str, max_len: int = 1900) -> List[str]:
    if len(text) <= max_len:
        return [text]
    out: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            out.append(remaining)
            break
        # try to split on a newline
        cut = remaining.rfind("\n", 0, max_len)
        if cut <= 0:
            cut = max_len
        out.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return out
