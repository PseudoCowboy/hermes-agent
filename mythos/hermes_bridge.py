"""Bridge between the hermes-agent Discord gateway and the Mythos
orchestrator.

This is the "build on top of hermes-agent's existing Discord gateway"
path mentioned in the task brief. Operators who already run a hermes
gateway can wire Mythos in as a callback rather than running the
``DiscordPyAdapter`` directly.

Usage (sketch):

    from gateway.platforms.discord import DiscordAdapter as HermesDiscord
    from mythos.hermes_bridge import HermesDiscordBridge
    from mythos.orchestrator import Orchestrator

    bridge = HermesDiscordBridge(hermes_adapter)
    orchestrator = Orchestrator(... discord=bridge)
    bridge.add_listener(orchestrator.handle_message)

The bridge implements the Mythos ``DiscordAdapter`` protocol by
forwarding sends through hermes's ``send_response`` API and mapping
inbound ``MessageEvent`` objects to ``DiscordMessage``.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence

from mythos.discord_adapter import ChannelListener, DiscordMessage

logger = logging.getLogger(__name__)


class HermesDiscordBridge:
    """Adapter glue around a hermes-agent ``DiscordAdapter`` instance.

    The hermes adapter gives us:
    * inbound ``MessageEvent`` objects via its message-handling pipeline
    * outbound message sending via ``send_response`` / channel posts
    * channel + category creation via the underlying discord.py client

    We expose just enough of that as the Mythos ``DiscordAdapter``
    Protocol.
    """

    def __init__(self, hermes_adapter: Any):
        self._hermes = hermes_adapter
        self._listeners: List[ChannelListener] = []

    # -- inbound -------------------------------------------------------

    def add_listener(self, listener: ChannelListener) -> None:
        self._listeners.append(listener)

    def deliver(self, event: Any) -> None:
        """Call this from the hermes adapter's pre-route hook."""
        message = self._event_to_message(event)
        if message is None:
            return
        for listener in list(self._listeners):
            try:
                listener(message)
            except Exception:
                logger.exception("listener raised")

    @staticmethod
    def _event_to_message(event: Any) -> Optional[DiscordMessage]:
        # hermes' MessageEvent has channel_id, sender_id, sender_name,
        # text, raw, etc. We only need the discord-channel id and a
        # plain-text body.
        try:
            return DiscordMessage(
                id=str(getattr(event, "message_id", "")),
                channel_id=str(getattr(event, "channel_id", "") or getattr(event, "channel", "")),
                author_id=str(getattr(event, "sender_id", "") or getattr(event, "user_id", "")),
                author_display_name=getattr(event, "sender_name", "") or "user",
                content=getattr(event, "text", "") or "",
                is_bot=bool(getattr(event, "is_bot", False)),
                mentions=_extract_at_tokens(getattr(event, "text", "") or ""),
            )
        except Exception:
            return None

    # -- outbound ------------------------------------------------------

    def send_message(
        self,
        *,
        channel_id: str,
        content: str,
        agent_name: Optional[str] = None,
        agent_display_name: Optional[str] = None,
        mention_user_ids: Optional[Sequence[str]] = None,
    ) -> DiscordMessage:
        # hermes adapters expose a send-text method but its name varies;
        # we look for the first one that exists on the wrapped object.
        for method in ("send_text", "send_message", "post", "send"):
            fn = getattr(self._hermes, method, None)
            if callable(fn):
                try:
                    fn(channel_id=channel_id, text=content, sender_display_name=agent_display_name)
                    break
                except TypeError:
                    try:
                        fn(channel_id, content)
                        break
                    except Exception:
                        continue
        return DiscordMessage(
            id="",
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

    def channel_history(self, *, channel_id: str, limit: int = 30) -> List[DiscordMessage]:
        fn = getattr(self._hermes, "fetch_history", None) or getattr(self._hermes, "get_history", None)
        if not callable(fn):
            return []
        try:
            raw = fn(channel_id=channel_id, limit=limit)
        except TypeError:
            raw = fn(channel_id, limit)
        out: List[DiscordMessage] = []
        for item in raw or []:
            out.append(
                DiscordMessage(
                    id=str(getattr(item, "id", "")),
                    channel_id=channel_id,
                    author_id=str(getattr(item, "author_id", "")),
                    author_display_name=getattr(item, "author_display_name", ""),
                    content=getattr(item, "content", "") or "",
                    is_bot=bool(getattr(item, "is_bot", False)),
                    mentions=_extract_at_tokens(getattr(item, "content", "") or ""),
                )
            )
        return out

    def create_category(self, *, guild_id: str, name: str) -> str:
        fn = getattr(self._hermes, "create_category", None)
        if not callable(fn):
            raise NotImplementedError("hermes adapter does not expose create_category")
        return str(fn(guild_id=guild_id, name=name))

    def create_sub_channel(self, *, guild_id: str, category_id: str, name: str) -> str:
        fn = getattr(self._hermes, "create_text_channel", None) or getattr(self._hermes, "create_channel", None)
        if not callable(fn):
            raise NotImplementedError("hermes adapter does not expose create_*channel")
        return str(fn(guild_id=guild_id, category_id=category_id, name=name))

    def emit_user_message(self, message: DiscordMessage) -> None:
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
