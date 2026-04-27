"""Discord admin + reaction-waiter helpers for the orchestration system.

Per ``plans/discord-orchestration-spec/02-technical-design.md``:

- §3 Reaction waiting model: waits keyed by ``(channel_id, message_id,
  user_id)``.  Reactions outside the keyed set are not delivered to a
  waiter; reactions during the *sentinel window* (between when an
  orchestrator posts a clarification and when the bot has registered
  its waiter) are buffered per-session by the caller, not by this
  module's global registry.
- §4 Admin tool boundary: this module exposes the three admin
  operations called by the orchestrator — create category, create
  channel, archive project — as plain async helpers.  The tool-call
  wrappers around these ship in P4 (``tools/discord_orchestration_tools.py``).
- §10 Safety: validation here is mistake resistance.  Names are
  Discord-side validated by the API too; we only prefilter to fail
  fast on obviously bad input.

The :class:`ReactionWaiter` is intentionally framework-agnostic: it
takes only an ``asyncio`` event loop and tuples of primitive ids, so
the test suite can drive it without a live bot.  The :class:`OrchestrationAdmin`
helpers take a discord.py ``Guild`` / ``CategoryChannel`` /
``TextChannel`` (duck-typed in tests).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Iterable, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# Discord channel name constraints (lowercased ASCII, hyphens, 1-100 chars).
# We enforce a *stricter* subset — printable ASCII without spaces or
# slashes — so a category id collision can never collide with a path
# segment we use elsewhere (e.g. .worktrees/<name>).
_CHANNEL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,99}$")
_CATEGORY_NAME_MAX = 100  # Discord limit on category names

# Default emojis the orchestrator commonly waits for.  Callers can
# override per-wait via the ``allowed_emojis`` argument.
DEFAULT_APPROVAL_EMOJIS: FrozenSet[str] = frozenset({"\u2705", "\u274c"})  # ✅ ❌


# =============================================================================
# Validation
# =============================================================================


class OrchestrationError(Exception):
    """Raised when an orchestration helper rejects an argument or fails."""


def _validate_channel_name(name: str, *, label: str = "name") -> str:
    if not isinstance(name, str) or not name:
        raise OrchestrationError(f"{label} must be a non-empty string")
    if not _CHANNEL_NAME_RE.match(name):
        raise OrchestrationError(
            f"{label} {name!r} must match {_CHANNEL_NAME_RE.pattern}; "
            "Discord channel names are lowercase letters, digits, '-', '_'"
        )
    return name


def _validate_category_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise OrchestrationError("category name must be a non-empty string")
    if len(name) > _CATEGORY_NAME_MAX:
        raise OrchestrationError(
            f"category name length {len(name)} exceeds Discord max "
            f"{_CATEGORY_NAME_MAX}"
        )
    return name


# =============================================================================
# Reaction waiter (§3)
# =============================================================================


@dataclass(frozen=True)
class ReactionKey:
    """Identity of a single reaction wait.

    ``user_id`` constrains *who* can satisfy the wait — typically the
    project owner.  Without it, any operator reacting in the channel
    would race the wait, breaking §3's promise of cross-session
    isolation when the same operator is approving prompts in multiple
    channels.
    """

    channel_id: str
    message_id: str
    user_id: str

    @classmethod
    def of(cls, channel_id: Any, message_id: Any, user_id: Any) -> "ReactionKey":
        return cls(str(channel_id), str(message_id), str(user_id))


@dataclass
class _PendingWait:
    future: "asyncio.Future[str]"
    allowed_emojis: FrozenSet[str]
    # Origin loop — used so a reaction event delivered on a different
    # loop (e.g. a discord.py event loop in another thread of the
    # gateway) hands the result back to the right Future without raising
    # ``InvalidStateError`` from the wrong thread.
    loop: asyncio.AbstractEventLoop


class ReactionWaiter:
    """Per-adapter registry of outstanding reaction waits.

    Single-shot: a waiter is removed as soon as it completes, is
    cancelled, or times out.  Re-keying with the same triple while a
    waiter is pending raises :class:`OrchestrationError` — that
    indicates two callers trying to wait on the same prompt at once,
    which is always a bug at this layer (§3 says one outstanding
    clarification per session).
    """

    def __init__(self) -> None:
        self._pending: Dict[ReactionKey, _PendingWait] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Caller side
    # ------------------------------------------------------------------

    async def wait(
        self,
        channel_id: Any,
        message_id: Any,
        user_id: Any,
        *,
        allowed_emojis: Optional[Iterable[str]] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """Block until the keyed reaction arrives or timeout fires.

        Returns the emoji string that satisfied the wait.  Raises
        :class:`asyncio.TimeoutError` on timeout and
        :class:`OrchestrationError` on duplicate registration.
        """
        key = ReactionKey.of(channel_id, message_id, user_id)
        emojis = (
            frozenset(allowed_emojis)
            if allowed_emojis is not None
            else DEFAULT_APPROVAL_EMOJIS
        )
        if not emojis:
            raise OrchestrationError("allowed_emojis must be non-empty")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        async with self._lock:
            if key in self._pending:
                raise OrchestrationError(
                    f"a wait is already pending for {key}; refusing to overwrite"
                )
            self._pending[key] = _PendingWait(
                future=future, allowed_emojis=emojis, loop=loop
            )

        try:
            if timeout is not None:
                return await asyncio.wait_for(future, timeout=timeout)
            return await future
        finally:
            # Clear under lock so a delivery race cannot resurrect a
            # completed wait into a stale entry.
            async with self._lock:
                self._pending.pop(key, None)

    # ------------------------------------------------------------------
    # Event-handler side (called from on_raw_reaction_add)
    # ------------------------------------------------------------------

    def deliver(
        self,
        *,
        channel_id: Any,
        message_id: Any,
        user_id: Any,
        emoji: str,
    ) -> bool:
        """Deliver a reaction event.  Returns True if a waiter accepted it.

        Safe to call from any thread — uses ``loop.call_soon_threadsafe``
        to set the Future on the waiter's origin loop.  Reactions that
        do not match a pending key are silently ignored; the §3 sentinel
        buffer lives on the caller's session, not here.
        """
        if not isinstance(emoji, str) or not emoji:
            return False
        key = ReactionKey.of(channel_id, message_id, user_id)
        # Snapshot under no lock — _pending is mutated by async tasks
        # only, and a stale read is OK: we recheck via Future state.
        wait = self._pending.get(key)
        if wait is None:
            return False
        if emoji not in wait.allowed_emojis:
            return False
        if wait.future.done():
            return False

        def _resolve() -> None:
            if not wait.future.done():
                wait.future.set_result(emoji)

        try:
            wait.loop.call_soon_threadsafe(_resolve)
        except RuntimeError:
            # Loop closed while we were dispatching — drop the event.
            return False
        return True

    # ------------------------------------------------------------------
    # Test / introspection
    # ------------------------------------------------------------------

    def pending_count(self) -> int:
        return len(self._pending)

    def has_pending(self, channel_id: Any, message_id: Any, user_id: Any) -> bool:
        return ReactionKey.of(channel_id, message_id, user_id) in self._pending


# =============================================================================
# Admin operations (§4 — category / channel / archive)
# =============================================================================


@dataclass(frozen=True)
class CreatedCategory:
    category_id: str
    name: str


@dataclass(frozen=True)
class CreatedChannel:
    channel_id: str
    category_id: str
    name: str


async def create_project_category(
    *,
    guild: Any,
    name: str,
    overwrites: Optional[Dict[Any, Any]] = None,
    reason: Optional[str] = None,
) -> CreatedCategory:
    """Create a Discord category to host one project's channels.

    The returned ``category_id`` is the ``scope_id`` used everywhere
    downstream (workflow storage, worktrees).  Caller is responsible
    for persisting the mapping.

    *guild* must expose ``create_category(name, overwrites=, reason=)``
    returning an awaitable with ``.id`` and ``.name`` — matches
    discord.py's ``Guild.create_category`` and the test fakes.
    """
    name = _validate_category_name(name)
    try:
        cat = await guild.create_category(
            name, overwrites=overwrites or {}, reason=reason
        )
    except Exception as exc:
        raise OrchestrationError(
            f"failed to create category {name!r}: {exc}"
        ) from exc
    return CreatedCategory(category_id=str(cat.id), name=cat.name)


async def create_stream_channel(
    *,
    category: Any,
    name: str,
    topic: Optional[str] = None,
    overwrites: Optional[Dict[Any, Any]] = None,
    reason: Optional[str] = None,
) -> CreatedChannel:
    """Create a text channel inside *category* for one stream.

    Per design §6, channel name and stream name share the same simple
    segment (e.g. ``frontend``, ``backend``); :func:`_validate_channel_name`
    enforces the format that's safe both for Discord and for use as a
    git worktree directory.
    """
    name = _validate_channel_name(name)
    if topic is not None and not isinstance(topic, str):
        raise OrchestrationError("topic must be a string or None")
    try:
        ch = await category.create_text_channel(
            name, topic=topic, overwrites=overwrites or {}, reason=reason
        )
    except Exception as exc:
        raise OrchestrationError(
            f"failed to create channel {name!r} in category "
            f"{getattr(category, 'id', '?')}: {exc}"
        ) from exc
    return CreatedChannel(
        channel_id=str(ch.id),
        category_id=str(getattr(category, "id", "?")),
        name=ch.name,
    )


async def archive_project_category(
    *,
    category: Any,
    reason: Optional[str] = None,
    channel_filter: Optional[Callable[[Any], bool]] = None,
) -> Dict[str, Any]:
    """Archive a project category by deleting its channels then itself.

    Discord has no native "archive category" primitive — the convention
    here is delete-and-record-for-audit.  The caller is expected to
    have already persisted whatever transcript/runstate they need; this
    helper does not snapshot anything.

    *channel_filter*, if given, gets called with each channel and must
    return True for channels that may be removed.  Defaults to all.
    """
    deleted_channels: list[str] = []
    skipped_channels: list[str] = []
    keep = channel_filter if channel_filter is not None else (lambda _ch: True)

    # Snapshot the channel list once — Discord paginates via attribute
    # so we don't want to iterate while mutating.
    channels = list(getattr(category, "channels", []) or [])
    for ch in channels:
        if not keep(ch):
            skipped_channels.append(str(getattr(ch, "id", "?")))
            continue
        try:
            await ch.delete(reason=reason)
            deleted_channels.append(str(getattr(ch, "id", "?")))
        except Exception as exc:
            raise OrchestrationError(
                f"failed to delete channel {getattr(ch, 'id', '?')}: {exc}"
            ) from exc

    if skipped_channels:
        # Caller asked us to keep some channels — leave the category
        # intact so they remain reachable.
        return {
            "category_id": str(getattr(category, "id", "?")),
            "deleted_channels": deleted_channels,
            "skipped_channels": skipped_channels,
            "category_deleted": False,
        }

    try:
        await category.delete(reason=reason)
    except Exception as exc:
        raise OrchestrationError(
            f"failed to delete category {getattr(category, 'id', '?')}: {exc}"
        ) from exc
    return {
        "category_id": str(getattr(category, "id", "?")),
        "deleted_channels": deleted_channels,
        "skipped_channels": [],
        "category_deleted": True,
    }


# =============================================================================
# Adapter integration helper
# =============================================================================


def install_reaction_handler(
    client: Any, waiter: ReactionWaiter, *, bot_user_id: Optional[str] = None
) -> Callable[[Any], Awaitable[None]]:
    """Register an ``on_raw_reaction_add`` handler on *client*.

    Returns the registered coroutine so callers (and tests) can drive
    it directly.  *bot_user_id*, if set, suppresses self-reactions —
    needed because a bot may add its own emoji to seed UI affordances.
    """

    @client.event
    async def on_raw_reaction_add(payload: Any) -> None:  # noqa: D401
        try:
            user_id = getattr(payload, "user_id", None)
            if user_id is None:
                return
            if bot_user_id is not None and str(user_id) == str(bot_user_id):
                return
            channel_id = getattr(payload, "channel_id", None)
            message_id = getattr(payload, "message_id", None)
            emoji = getattr(payload, "emoji", None)
            emoji_str = str(emoji) if emoji is not None else ""
            if not channel_id or not message_id or not emoji_str:
                return
            waiter.deliver(
                channel_id=channel_id,
                message_id=message_id,
                user_id=user_id,
                emoji=emoji_str,
            )
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("on_raw_reaction_add handler failed")

    return on_raw_reaction_add
