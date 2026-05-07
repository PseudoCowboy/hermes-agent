"""Optional bridge: run Mythos on top of the hermes-agent Discord adapter.

Use this when you want a single hermes process to handle both its
normal flows and Mythos project orchestration on the same bot token.
The bridge translates hermes' ``MessageEvent`` into Mythos'
``IncomingMessage`` and delegates outbound posts back to hermes'
``DiscordAdapter.send``.

Operators who don't already run hermes can ignore this module — the
plain ``python -m mythos`` entrypoint stands alone and uses
``discord.py`` directly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from mythos.discord_io import DiscordIO, IncomingMessage, MessageHandler


logger = logging.getLogger("mythos.hermes_bridge")


class HermesDiscordIO:
    """Adapt a hermes ``DiscordAdapter`` to the Mythos ``DiscordIO`` Protocol.

    Constructed with an existing ``DiscordAdapter`` instance. Forwards
    ``send`` to the adapter's send method, and subscribes to its
    ``on_message`` events by patching a callback in.
    """

    def __init__(self, adapter):
        self._adapter = adapter
        self._handler: Optional[MessageHandler] = None

    def on_message(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def start(self) -> None:
        # Hermes already starts the adapter elsewhere; nothing to do.
        return None

    async def close(self) -> None:
        return None

    async def feed_event(self, event) -> None:
        """Hermes' main loop calls this for each MessageEvent.

        Convert and dispatch into Mythos. Errors are logged and dropped
        to avoid taking down the hermes loop.
        """
        if self._handler is None:
            return
        try:
            src = event.source
            await self._handler(
                IncomingMessage(
                    channel_id=int(src.chat_id),
                    user_id=int(src.user_id or 0),
                    user_name=str(getattr(src, "user_name", "") or "user"),
                    content=str(event.text or ""),
                    message_id=int(event.message_id or 0) if event.message_id else 0,
                )
            )
        except Exception:
            logger.exception("mythos bridge: failed to dispatch hermes event")

    async def send(self, channel_id: int, content: str) -> int:
        result = await self._adapter.send(str(channel_id), content)
        if not result.success:
            raise RuntimeError(
                f"hermes send to channel {channel_id} failed: {result.error}"
            )
        return int(result.message_id or 0)

    async def create_category(self, guild_id: int, name: str) -> int:
        # Hermes' adapter does not expose channel creation directly, so
        # we reach into its discord client.
        client = getattr(self._adapter, "_client", None)
        if client is None:
            raise RuntimeError("hermes adapter has no live discord client")
        guild = client.get_guild(guild_id) or await client.fetch_guild(guild_id)
        cat = await guild.create_category(name=name)
        return int(cat.id)

    async def create_text_channel(
        self, guild_id: int, category_id: int, name: str
    ) -> int:
        client = getattr(self._adapter, "_client", None)
        if client is None:
            raise RuntimeError("hermes adapter has no live discord client")
        guild = client.get_guild(guild_id) or await client.fetch_guild(guild_id)
        category = guild.get_channel(category_id)
        ch = await guild.create_text_channel(name=name, category=category)
        return int(ch.id)
