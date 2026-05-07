"""In-memory Discord double for tests.

Implements :class:`mythos.discord_client.DiscordClient` without touching the
network. Tests drive it with :meth:`user_says` and assert against
:attr:`channel_log`.
"""

from __future__ import annotations

import asyncio
import itertools
from collections import defaultdict
from typing import Awaitable, Callable, Dict, List, Optional

from mythos.discord_client import IncomingMessage, MessageHandler


class FakeDiscordClient:
    def __init__(self, main_channel_id: int = 1000) -> None:
        self.main_channel_id = main_channel_id
        self._handler: Optional[MessageHandler] = None
        self._next_channel = itertools.count(2000)
        self._next_message = itertools.count(10000)
        self.channels: Dict[int, Dict[str, str]] = {
            main_channel_id: {"name": "main", "topic": "main intake"}
        }
        # channel_id -> list[(author_name, content)]
        self.channel_log: Dict[int, List[tuple[str, str]]] = defaultdict(list)
        self.bot_user_id = 999  # any messages we send have this author id

    # ------------------------------------------------------------------
    # DiscordClient protocol

    def on_message(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def send_message(self, channel_id: int, content: str) -> int:
        if channel_id not in self.channels:
            raise RuntimeError(f"send to unknown channel {channel_id}")
        msg_id = next(self._next_message)
        self.channel_log[channel_id].append(("BOT", content))
        # Loop the bot's own messages back so guard logic can be tested.
        if self._handler is not None:
            await self._handler(
                IncomingMessage(
                    message_id=msg_id,
                    channel_id=channel_id,
                    author_id=self.bot_user_id,
                    author_name="BOT",
                    content=content,
                    is_bot=True,
                )
            )
        return msg_id

    async def create_project_channel(
        self, name: str, topic: Optional[str] = None
    ) -> int:
        return self._make_channel(name, topic, "project")

    async def create_task_channel(
        self,
        project_channel_name: str,
        kind: str,
        topic: Optional[str] = None,
    ) -> int:
        return self._make_channel(f"{project_channel_name}-{kind}", topic, kind)

    def _make_channel(self, name: str, topic: Optional[str], kind: str) -> int:
        cid = next(self._next_channel)
        self.channels[cid] = {"name": name, "topic": topic or "", "kind": kind}
        return cid

    # ------------------------------------------------------------------
    # Test helpers

    async def user_says(
        self,
        content: str,
        *,
        channel_id: Optional[int] = None,
        author_id: int = 1,
        author_name: str = "user",
    ) -> int:
        cid = channel_id if channel_id is not None else self.main_channel_id
        if cid not in self.channels:
            raise RuntimeError(f"unknown channel {cid}")
        msg_id = next(self._next_message)
        self.channel_log[cid].append((author_name, content))
        if self._handler is not None:
            await self._handler(
                IncomingMessage(
                    message_id=msg_id,
                    channel_id=cid,
                    author_id=author_id,
                    author_name=author_name,
                    content=content,
                    is_bot=False,
                )
            )
        return msg_id

    def messages_in(self, channel_id: int) -> List[tuple[str, str]]:
        return list(self.channel_log[channel_id])

    def channel_kind(self, channel_id: int) -> str:
        return self.channels.get(channel_id, {}).get("kind", "")

    def channels_of_kind(self, kind: str) -> List[int]:
        return [
            cid for cid, info in self.channels.items() if info.get("kind") == kind
        ]
