"""In-memory Discord double used by integration tests."""

from __future__ import annotations

import threading
import uuid
from typing import Dict, List, Optional, Sequence

from mythos.discord_adapter import ChannelListener, DiscordMessage


class InMemoryDiscord:
    """A test-friendly Discord stand-in.

    * ``create_category`` / ``create_sub_channel`` allocate string ids.
    * ``send_message`` appends to per-channel history and triggers
      listeners (so agent-to-agent pings can be observed).
    * ``emit_user_message`` is the synthetic 'human user typed something'
      hook tests use.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._categories: Dict[str, Dict[str, str]] = {}
        self._channels: Dict[str, Dict[str, str]] = {}  # channel_id -> {name, category_id, guild_id}
        self._channel_history: Dict[str, List[DiscordMessage]] = {}
        self._listeners: List[ChannelListener] = []

    # Channels --------------------------------------------------------

    def create_category(self, *, guild_id: str, name: str) -> str:
        with self._lock:
            cid = f"cat_{uuid.uuid4().hex[:8]}"
            self._categories[cid] = {"name": name, "guild_id": guild_id}
            return cid

    def create_sub_channel(self, *, guild_id: str, category_id: str, name: str) -> str:
        with self._lock:
            chid = f"ch_{uuid.uuid4().hex[:8]}"
            self._channels[chid] = {"name": name, "category_id": category_id, "guild_id": guild_id}
            self._channel_history[chid] = []
            return chid

    def add_loose_channel(self, *, guild_id: str, name: str = "main") -> str:
        """Helper: make a channel that isn't under any category (the main channel)."""
        with self._lock:
            chid = f"ch_{uuid.uuid4().hex[:8]}"
            self._channels[chid] = {"name": name, "category_id": "", "guild_id": guild_id}
            self._channel_history[chid] = []
            return chid

    # Messages --------------------------------------------------------

    def send_message(
        self,
        *,
        channel_id: str,
        content: str,
        agent_name: Optional[str] = None,
        agent_display_name: Optional[str] = None,
        mention_user_ids: Optional[Sequence[str]] = None,
    ) -> DiscordMessage:
        with self._lock:
            msg_id = f"msg_{uuid.uuid4().hex[:8]}"
            msg = DiscordMessage(
                id=msg_id,
                channel_id=channel_id,
                author_id=agent_name or "bot",
                author_display_name=agent_display_name or agent_name or "Bot",
                content=content,
                is_bot=True,
                is_agent=agent_name is not None,
                agent_name=agent_name,
                mentions=_parse_mentions(content),
                user_mentions=list(mention_user_ids or []),
            )
            self._channel_history.setdefault(channel_id, []).append(msg)
        # send_message does NOT trigger listeners. Listeners fire from
        # emit_user_message (real-user input) only; bot/agent posts don't
        # need to round-trip through the listener path because the
        # orchestrator explicitly drives agent->agent handoffs.
        return msg

    def channel_history(self, *, channel_id: str, limit: int = 30) -> List[DiscordMessage]:
        with self._lock:
            return list(self._channel_history.get(channel_id, []))[-limit:]

    # Listeners / synthetic input ------------------------------------

    def add_listener(self, listener: ChannelListener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def emit_user_message(self, message: DiscordMessage) -> None:
        with self._lock:
            self._channel_history.setdefault(message.channel_id, []).append(message)
            listeners = list(self._listeners)
        for listener in listeners:
            listener(message)

    # Convenience for tests -------------------------------------------

    def channel_name(self, channel_id: str) -> str:
        return self._channels.get(channel_id, {}).get("name", "")

    def all_channels(self) -> Dict[str, Dict[str, str]]:
        with self._lock:
            return dict(self._channels)


def _parse_mentions(content: str) -> List[str]:
    """Extract `@Name` tokens. Names are case-insensitive but we preserve case."""
    out: List[str] = []
    i = 0
    while i < len(content):
        if content[i] == "@":
            j = i + 1
            while j < len(content) and (content[j].isalnum() or content[j] in ("_",)):
                j += 1
            token = content[i + 1 : j]
            if token:
                out.append(token)
            i = j
        else:
            i += 1
    return out
