"""discord.py adapter for production.

This is the only module in mythos that imports the ``discord`` library; it
keeps that dependency optional. The ``Orchestrator`` itself talks only to the
:class:`mythos.discord_client.DiscordClient` Protocol, so unit tests don't need
discord.py installed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from mythos.discord_client import IncomingMessage, MessageHandler


logger = logging.getLogger("mythos.discord_py")


class DiscordPyClient:
    def __init__(
        self,
        token: str,
        guild_id: int,
        main_channel_id: int,
        project_category_id: Optional[int] = None,
    ) -> None:
        try:
            import discord  # noqa: F401
            from discord.ext import commands  # noqa: F401
        except ImportError as exc:  # pragma: no cover - optional dep
            raise RuntimeError(
                "discord.py is required for DiscordPyClient. Install hermes-agent[messaging]."
            ) from exc
        import discord
        from discord.ext import commands

        self._discord = discord
        self._token = token
        self.guild_id = guild_id
        self.main_channel_id = main_channel_id
        self.project_category_id = project_category_id
        self._handler: Optional[MessageHandler] = None

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.guild_messages = True
        self._bot = commands.Bot(command_prefix="!", intents=intents)
        self._register_events()

    # ------------------------------------------------------------------
    # DiscordClient protocol

    def on_message(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def send_message(self, channel_id: int, content: str) -> int:
        channel = self._bot.get_channel(channel_id) or await self._bot.fetch_channel(
            channel_id
        )
        msg = await channel.send(content)
        return msg.id

    async def create_project_channel(
        self, name: str, topic: Optional[str] = None
    ) -> int:
        guild = self._bot.get_guild(self.guild_id)
        if guild is None:
            guild = await self._bot.fetch_guild(self.guild_id)
        category = None
        if self.project_category_id is not None:
            category = guild.get_channel(self.project_category_id)
        channel = await guild.create_text_channel(
            name=name, topic=topic, category=category
        )
        return channel.id

    async def create_task_channel(
        self,
        project_channel_name: str,
        kind: str,
        topic: Optional[str] = None,
    ) -> int:
        return await self.create_project_channel(
            f"{project_channel_name}-{kind}", topic=topic
        )

    # ------------------------------------------------------------------
    # Lifecycle

    async def start(self) -> None:
        await self._bot.start(self._token)

    async def close(self) -> None:
        await self._bot.close()

    # ------------------------------------------------------------------

    def _register_events(self) -> None:
        @self._bot.event
        async def on_ready():  # pragma: no cover - network-dependent
            logger.info("Mythos bot online as %s", self._bot.user)

        @self._bot.event
        async def on_message(message):  # pragma: no cover - network-dependent
            if self._handler is None:
                return
            try:
                await self._handler(
                    IncomingMessage(
                        message_id=message.id,
                        channel_id=message.channel.id,
                        author_id=message.author.id,
                        author_name=str(message.author),
                        content=message.content or "",
                        is_bot=bool(message.author.bot),
                    )
                )
            except Exception:
                logger.exception("orchestrator raised on message %s", message.id)
