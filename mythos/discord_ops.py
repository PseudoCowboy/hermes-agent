"""Abstract Discord operations the orchestrator depends on.

Tests inject a `FakeDiscordClient`; production wires `DiscordpyClient`
which uses discord.py.
"""
from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Protocol


@dataclass
class PostedMessage:
    channel_id: int
    author_id: int
    content: str
    message_id: int
    is_bot: bool = False


# Callback that the orchestrator registers; called whenever a *user* posts a
# message in a channel the orchestrator cares about.
MessageHandler = Callable[[PostedMessage], Awaitable[None]]


class DiscordOps(Protocol):
    """Minimum surface the orchestrator needs from Discord."""

    async def create_category(self, guild_id: int, name: str) -> int: ...
    async def create_text_channel(self, guild_id: int, name: str,
                                   category_id: Optional[int]) -> int: ...
    async def send(self, channel_id: int, content: str) -> int:
        """Post a message; return Discord message id."""
        ...
    def register_handler(self, handler: MessageHandler) -> None: ...


# ---------------------------------------------------------------------------


class FakeDiscordClient:
    """In-memory Discord double used by tests.

    Records categories, channels, and messages. Lets a test simulate user
    messages by calling `simulate_user_message`.
    """

    def __init__(self) -> None:
        self._next_id = 1000
        self.categories: Dict[int, str] = {}
        self.channels: Dict[int, dict] = {}  # id -> {name, category_id}
        self.messages: List[PostedMessage] = []
        self._handler: Optional[MessageHandler] = None

    def _alloc(self) -> int:
        self._next_id += 1
        return self._next_id

    async def create_category(self, guild_id: int, name: str) -> int:
        cid = self._alloc()
        self.categories[cid] = name
        return cid

    async def create_text_channel(self, guild_id: int, name: str,
                                   category_id: Optional[int]) -> int:
        cid = self._alloc()
        self.channels[cid] = {"name": name, "category_id": category_id, "guild_id": guild_id}
        return cid

    async def send(self, channel_id: int, content: str) -> int:
        mid = self._alloc()
        self.messages.append(PostedMessage(
            channel_id=channel_id,
            author_id=0,           # bot
            content=content,
            message_id=mid,
            is_bot=True,
        ))
        return mid

    def register_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    # ---- Test helpers ---------------------------------------------------

    async def simulate_user_message(self, channel_id: int, author_id: int,
                                    content: str) -> int:
        mid = self._alloc()
        msg = PostedMessage(
            channel_id=channel_id, author_id=author_id, content=content,
            message_id=mid, is_bot=False,
        )
        self.messages.append(msg)
        if self._handler:
            await self._handler(msg)
        return mid

    def messages_in(self, channel_id: int) -> List[PostedMessage]:
        return [m for m in self.messages if m.channel_id == channel_id]


# ---------------------------------------------------------------------------
# Real discord.py-backed client. Imported lazily so tests don't require a
# Discord connection.


class DiscordpyClient(abc.ABC):  # pragma: no cover - thin wrapper
    """Lazy-loaded discord.py client.

    The actual implementation lives in `mythos.discord_bot` to keep the
    discord.py import isolated. This base exists only so type checkers can
    see the contract.
    """

    @abc.abstractmethod
    async def create_category(self, guild_id: int, name: str) -> int: ...
    @abc.abstractmethod
    async def create_text_channel(self, guild_id: int, name: str,
                                   category_id: Optional[int]) -> int: ...
    @abc.abstractmethod
    async def send(self, channel_id: int, content: str) -> int: ...
    @abc.abstractmethod
    def register_handler(self, handler: MessageHandler) -> None: ...
