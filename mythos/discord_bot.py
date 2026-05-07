"""discord.py-backed transport for Mythos.

This module is intentionally lazy-imports `discord` so unit tests that use
`FakeDiscordClient` don't need a Discord connection.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .discord_ops import MessageHandler, PostedMessage


log = logging.getLogger("mythos.discord_bot")


class DiscordpyClient:
    """Real discord.py implementation of `DiscordOps`.

    Construction does NOT connect — call `start()` (or run via `run_bot()`)
    inside an asyncio event loop.
    """

    def __init__(self, token: str, guild_id: int, main_channel_id: int):
        import discord  # type: ignore
        from discord.ext import commands  # type: ignore

        self._discord = discord
        self.token = token
        self.guild_id = int(guild_id)
        self.main_channel_id = int(main_channel_id)
        self._handler: Optional[MessageHandler] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.guild_messages = True
        self.client = commands.Bot(command_prefix="!", intents=intents)

        @self.client.event
        async def on_ready():  # noqa: ANN001
            log.info("mythos discord client ready as %s", self.client.user)

        @self.client.event
        async def on_message(message):  # noqa: ANN001
            if message.author == self.client.user:
                return
            if self._handler is None:
                return
            posted = PostedMessage(
                channel_id=int(message.channel.id),
                author_id=int(message.author.id),
                content=str(message.content or ""),
                message_id=int(message.id),
                is_bot=bool(message.author.bot),
            )
            try:
                await self._handler(posted)
            except Exception:
                log.exception("mythos message handler failed")

    # ---- DiscordOps methods --------------------------------------------

    async def create_category(self, guild_id: int, name: str) -> int:
        guild = self.client.get_guild(guild_id) or await self.client.fetch_guild(guild_id)
        cat = await guild.create_category(name=name)
        return int(cat.id)

    async def create_text_channel(self, guild_id: int, name: str,
                                   category_id: Optional[int]) -> int:
        guild = self.client.get_guild(guild_id) or await self.client.fetch_guild(guild_id)
        category = None
        if category_id is not None:
            category = guild.get_channel(category_id)
        ch = await guild.create_text_channel(name=name, category=category)
        return int(ch.id)

    async def send(self, channel_id: int, content: str) -> int:
        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)
        msg = await channel.send(content)
        return int(msg.id)

    def register_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    # ---- Lifecycle ------------------------------------------------------

    async def start(self) -> None:
        await self.client.start(self.token)

    async def close(self) -> None:
        await self.client.close()
