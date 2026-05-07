"""Discord adapter interface used by the orchestrator.

Anything that can post messages, create categories/sub-channels, listen
for channel messages, and parse @mentions can be a ``DiscordAdapter``.
The production implementation in ``mythos.discord_real`` wraps the
hermes-agent Discord platform (which itself wraps discord.py); the
``InMemoryDiscord`` test double in ``mythos.fake_discord`` stores
messages in memory.

The adapter purposely does NOT know about projects, agents, or runners
- it deals only in raw Discord primitives.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence


@dataclass
class DiscordMessage:
    """An inbound or outbound Discord message."""

    id: str
    channel_id: str
    author_id: str  # id of poster (bot's own id, agent identity id, or user id)
    author_display_name: str
    content: str
    is_bot: bool = False
    is_agent: bool = False  # True when authored by one of our agents
    agent_name: Optional[str] = None  # the mythos agent name, if known
    mentions: List[str] = field(default_factory=list)  # display-name mentions (no @)
    user_mentions: List[str] = field(default_factory=list)  # raw user ids
    reactions: List[str] = field(default_factory=list)  # emoji shorthands ("white_check_mark", "thumbs_up")
    created_at: float = field(default_factory=time.time)


# Listener signature: takes a message and a context dict (chiefly the Discord
# adapter for follow-up actions and the project store for routing).
ChannelListener = Callable[[DiscordMessage], None]


class DiscordAdapter(Protocol):
    """Minimal Discord surface needed by Mythos."""

    # Channels --------------------------------------------------------

    def create_category(self, *, guild_id: str, name: str) -> str: ...

    def create_sub_channel(self, *, guild_id: str, category_id: str, name: str) -> str: ...

    # Messages --------------------------------------------------------

    def send_message(
        self,
        *,
        channel_id: str,
        content: str,
        agent_name: Optional[str] = None,
        agent_display_name: Optional[str] = None,
        mention_user_ids: Optional[Sequence[str]] = None,
    ) -> DiscordMessage: ...

    def channel_history(self, *, channel_id: str, limit: int = 30) -> List[DiscordMessage]: ...

    # Routing --------------------------------------------------------

    def add_listener(self, listener: ChannelListener) -> None: ...

    def emit_user_message(self, message: DiscordMessage) -> None:
        """Test helper: deliver a synthetic user message to listeners.

        Real adapters won't expose this; the in-memory fake does."""
        ...
