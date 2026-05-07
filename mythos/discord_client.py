"""Discord-facing surface for Mythos.

The orchestrator never imports ``discord`` directly — it talks to the
``DiscordClient`` Protocol defined here. Production wiring uses the
``DiscordPyClient`` adapter (which thinly wraps discord.py and reuses the same
pattern as hermes-agent's ``gateway/platforms/discord.py``). Tests use the
``FakeDiscordClient`` from ``mythos.testing.fakes``.

Message-handling responsibility:
- The client *receives* messages and forwards them to the orchestrator's
  ``on_message`` coroutine.
- The orchestrator *creates* channels and *sends* messages by calling back
  into the client.

This split keeps the orchestrator's logic platform-agnostic and easy to test.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Protocol


@dataclass
class IncomingMessage:
    """A message received from Discord (or a test fake)."""

    message_id: int
    channel_id: int
    author_id: int
    author_name: str
    content: str
    is_bot: bool = False


MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class DiscordClient(Protocol):
    """Minimal surface the orchestrator needs from Discord."""

    main_channel_id: int

    def on_message(self, handler: MessageHandler) -> None:
        """Register the orchestrator's message handler."""
        ...

    async def send_message(self, channel_id: int, content: str) -> int:
        """Send ``content`` to ``channel_id``; return the new message ID."""
        ...

    async def create_project_channel(
        self, name: str, topic: Optional[str] = None
    ) -> int:
        """Create a new project channel in the configured guild/category."""
        ...

    async def create_task_channel(
        self,
        project_channel_name: str,
        kind: str,
        topic: Optional[str] = None,
    ) -> int:
        """Create a per-task channel (frontend/backend/test).

        ``project_channel_name`` is the parent project's slug — used to derive
        a name like ``<slug>-frontend``.
        """
        ...


# ---------------------------------------------------------------------------
# Helpers shared by all clients

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def slugify(text: str, max_len: int = 32) -> str:
    """Discord channel-name-safe slug.

    Discord channel names must be 1-100 chars, lowercase, no spaces, no most
    punctuation. We trim aggressively for readability.
    """
    text = text.strip().lower().replace(" ", "-").replace("_", "-")
    text = _SLUG_RE.sub("", text)
    text = re.sub(r"-+", "-", text).strip("-")
    if not text:
        text = "project"
    return text[:max_len].rstrip("-") or "project"


def chunk_message(content: str, max_chars: int) -> List[str]:
    """Split a long message into Discord-safe chunks at line boundaries."""
    if len(content) <= max_chars:
        return [content]
    chunks: List[str] = []
    buf: List[str] = []
    size = 0
    for line in content.splitlines(keepends=True):
        if size + len(line) > max_chars and buf:
            chunks.append("".join(buf))
            buf = [line]
            size = len(line)
        else:
            buf.append(line)
            size += len(line)
    if buf:
        chunks.append("".join(buf))
    # Hard-split any chunk that is still too long (single very long line).
    final: List[str] = []
    for c in chunks:
        if len(c) <= max_chars:
            final.append(c)
        else:
            for i in range(0, len(c), max_chars):
                final.append(c[i : i + max_chars])
    return final
