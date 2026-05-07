"""Production Discord adapter built on ``discord.py`` (the same library
hermes-agent's Discord platform uses).

This is intentionally minimal:

* It owns its own bot client and gateway connection.
* It delegates per-agent identity to a single Discord webhook per
  channel (so each agent appears with its own display name + avatar
  without needing N separate bot tokens).
* It bridges incoming ``messageCreate`` events into the orchestrator's
  ``handle_message`` method by adapting them to ``DiscordMessage``.

When you would rather mount Mythos *inside* an existing hermes-agent
gateway process, see ``mythos.hermes_bridge`` for a thin wrapper that
turns a ``BasePlatformAdapter`` event into a ``DiscordMessage`` and
sends responses through the platform's outbound delivery path.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Awaitable, Callable, Dict, List, Optional, Sequence

from mythos.discord_adapter import ChannelListener, DiscordMessage

logger = logging.getLogger(__name__)


try:
    import discord  # type: ignore
    DISCORD_AVAILABLE = True
except ImportError:  # pragma: no cover
    discord = None
    DISCORD_AVAILABLE = False


class DiscordPyAdapter:
    """Minimal direct discord.py wrapper.

    Note: the orchestrator runs synchronously; this adapter runs async
    on its own event loop in a background thread and posts the inbound
    messages to ``handle_message`` via a thread-safe queue. For the
    purposes of the integration tests we use ``InMemoryDiscord``
    instead.
    """

    def __init__(self, *, token: str, guild_id: int, intents=None):
        if not DISCORD_AVAILABLE:
            raise RuntimeError("discord.py is not installed; pip install discord.py")
        self._token = token
        self._guild_id = guild_id
        self._listeners: List[ChannelListener] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client: Optional["discord.Client"] = None  # type: ignore
        self._intents = intents or discord.Intents.default()
        self._intents.message_content = True
        self._intents.guild_messages = True
        self._intents.reactions = True
        self._webhook_cache: Dict[str, "discord.Webhook"] = {}  # type: ignore
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    # ------------------------------------------------------------------ start

    def start(self) -> None:
        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._client = discord.Client(intents=self._intents)
            self._register_handlers()
            self._loop.run_until_complete(self._client.start(self._token))

        self._thread = threading.Thread(target=_run, name="mythos-discord", daemon=True)
        self._thread.start()
        # Caller should wait for ready via .wait_until_ready() if needed.

    def wait_until_ready(self, timeout: Optional[float] = None) -> bool:
        return self._ready.wait(timeout)

    def stop(self) -> None:
        if self._loop and self._client:
            asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)

    def _register_handlers(self) -> None:
        client = self._client

        @client.event
        async def on_ready():  # type: ignore
            self._ready.set()
            logger.info("Mythos discord adapter ready as %s", client.user)

        @client.event
        async def on_message(message):  # type: ignore
            if message.author.id == client.user.id:
                return
            wrapped = DiscordMessage(
                id=str(message.id),
                channel_id=str(message.channel.id),
                author_id=str(message.author.id),
                author_display_name=message.author.display_name,
                content=message.content or "",
                is_bot=message.author.bot,
                is_agent=False,
                mentions=_extract_at_tokens(message.content or ""),
                user_mentions=[str(u.id) for u in (message.mentions or [])],
            )
            for listener in list(self._listeners):
                try:
                    listener(wrapped)
                except Exception:
                    logger.exception("listener failed")

        @client.event
        async def on_reaction_add(reaction, user):  # type: ignore
            if user.id == client.user.id:
                return
            wrapped = DiscordMessage(
                id=str(reaction.message.id),
                channel_id=str(reaction.message.channel.id),
                author_id=str(user.id),
                author_display_name=user.display_name,
                content=reaction.message.content or "",
                is_bot=False,
                reactions=[str(reaction.emoji)],
            )
            for listener in list(self._listeners):
                try:
                    listener(wrapped)
                except Exception:
                    logger.exception("listener failed")

    # ------------------------------------------------------------------ ops

    def create_category(self, *, guild_id: str, name: str) -> str:
        fut = asyncio.run_coroutine_threadsafe(self._create_category_async(int(guild_id), name), self._loop)
        return str(fut.result())

    async def _create_category_async(self, guild_id: int, name: str) -> int:
        guild = self._client.get_guild(guild_id) or await self._client.fetch_guild(guild_id)
        category = await guild.create_category(name=name)
        return category.id

    def create_sub_channel(self, *, guild_id: str, category_id: str, name: str) -> str:
        fut = asyncio.run_coroutine_threadsafe(
            self._create_subchannel_async(int(guild_id), int(category_id), name), self._loop
        )
        return str(fut.result())

    async def _create_subchannel_async(self, guild_id: int, category_id: int, name: str) -> int:
        guild = self._client.get_guild(guild_id) or await self._client.fetch_guild(guild_id)
        category = guild.get_channel(category_id)
        channel = await guild.create_text_channel(name=name, category=category)
        return channel.id

    def send_message(
        self,
        *,
        channel_id: str,
        content: str,
        agent_name: Optional[str] = None,
        agent_display_name: Optional[str] = None,
        mention_user_ids: Optional[Sequence[str]] = None,
    ) -> DiscordMessage:
        fut = asyncio.run_coroutine_threadsafe(
            self._send_async(int(channel_id), content, agent_display_name, mention_user_ids), self._loop
        )
        msg_id = fut.result()
        return DiscordMessage(
            id=str(msg_id),
            channel_id=channel_id,
            author_id=agent_name or "bot",
            author_display_name=agent_display_name or agent_name or "Bot",
            content=content,
            is_bot=True,
            is_agent=agent_name is not None,
            agent_name=agent_name,
            mentions=_extract_at_tokens(content),
            user_mentions=list(mention_user_ids or []),
        )

    async def _send_async(
        self,
        channel_id: int,
        content: str,
        display_name: Optional[str],
        mention_user_ids: Optional[Sequence[str]],
    ) -> int:
        channel = self._client.get_channel(channel_id) or await self._client.fetch_channel(channel_id)
        # Use channel webhook for per-agent identity if a display_name is provided.
        if display_name:
            webhook = await self._get_or_create_webhook(channel)
            msg = await webhook.send(content=content, username=display_name, wait=True)
            return msg.id
        msg = await channel.send(content=content)
        return msg.id

    async def _get_or_create_webhook(self, channel):
        cache_key = str(channel.id)
        if cache_key in self._webhook_cache:
            return self._webhook_cache[cache_key]
        for hook in await channel.webhooks():
            if hook.name == "mythos-agents":
                self._webhook_cache[cache_key] = hook
                return hook
        hook = await channel.create_webhook(name="mythos-agents")
        self._webhook_cache[cache_key] = hook
        return hook

    def channel_history(self, *, channel_id: str, limit: int = 30) -> List[DiscordMessage]:
        fut = asyncio.run_coroutine_threadsafe(
            self._channel_history_async(int(channel_id), limit), self._loop
        )
        return fut.result()

    async def _channel_history_async(self, channel_id: int, limit: int) -> List[DiscordMessage]:
        channel = self._client.get_channel(channel_id) or await self._client.fetch_channel(channel_id)
        out: List[DiscordMessage] = []
        async for msg in channel.history(limit=limit):
            out.append(
                DiscordMessage(
                    id=str(msg.id),
                    channel_id=str(channel_id),
                    author_id=str(msg.author.id),
                    author_display_name=msg.author.display_name,
                    content=msg.content or "",
                    is_bot=msg.author.bot,
                    mentions=_extract_at_tokens(msg.content or ""),
                )
            )
        return list(reversed(out))

    def add_listener(self, listener: ChannelListener) -> None:
        self._listeners.append(listener)

    def emit_user_message(self, message: DiscordMessage) -> None:  # pragma: no cover
        # Not used in production.
        for listener in list(self._listeners):
            listener(message)


def _extract_at_tokens(content: str) -> List[str]:
    out: List[str] = []
    i = 0
    while i < len(content):
        if content[i] == "@":
            j = i + 1
            while j < len(content) and (content[j].isalnum() or content[j] == "_"):
                j += 1
            token = content[i + 1 : j]
            if token:
                out.append(token)
            i = j
        else:
            i += 1
    return out
