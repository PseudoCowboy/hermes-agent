"""Per-channel long-lived agent sessions + a tiny router.

Per ``plans/discord-orchestration-spec/01-product-spec.md``:

- §1 Project isolation: each Discord channel is a long-lived agent
  session keyed by ``(scope_id, slug, stream_name)``.  P5 lands the
  in-memory representation of that session; production registration
  of channels into the router lands in P6 (orchestrator-driven).
- §2 Clarification one-shot admission: when an agent has posted a
  clarification prompt, exactly one subsequent user message in the
  same channel is admitted as the answer; further messages buffer
  until the next outstanding prompt.  Concurrent deliveries to the
  same channel must serialize.
- §5 Stream channels: this module is the per-channel admission gate.
  Routing and admission only — agent spawn / persona / handoff land
  in P6 / P7.

The :class:`LongLivedSession` deliberately avoids touching
:class:`gateway.session.SessionEntry` / :class:`SessionStore`: those
remain the per-channel persistent SQLite layer.  This object is
in-memory state for the live agent loop and is rebuilt on restart.

The three-state ``_pending_prompt_message_id`` generalises the
"admit at most one downstream consumer per outstanding prompt"
pattern that ``GatewayRunner._update_prompt_pending`` already uses,
to handle the race between *the agent requesting a clarification*
and *the prompt actually appearing on Discord* — messages arriving
in that window must NOT be silently consumed as the answer.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Bounded inbox depth.  A long-lived session that's already saturated
# usually means the agent loop is wedged; dropping new messages with a
# warning surfaces the wedge instead of growing memory unboundedly.
DEFAULT_INBOX_MAXSIZE = 64


# Module-level sentinel: clarification has been *requested* by the
# agent but the prompt message id is not yet known (the post hasn't
# completed).  See "three-state" docstring on ``LongLivedSession``.
class _PromptPendingSentinel:
    """Singleton type for the SENTINEL_PENDING marker."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<SENTINEL_PENDING>"


SENTINEL_PENDING: _PromptPendingSentinel = _PromptPendingSentinel()


# =============================================================================
# Inbox payload
# =============================================================================


@dataclass
class InboundMessage:
    """One message handed to a :class:`LongLivedSession` from the gateway.

    *kind* distinguishes a normal user message from a clarification
    answer; the agent loop (P6+) reads it to know whether to resume
    a paused turn or to start a new one.

    *event* is the raw ``MessageEvent`` from the gateway, kept opaque
    so this module has no import dependency on ``gateway.run``.
    """

    kind: str  # "message" | "clarification_reply"
    event: Any
    prompt_message_id: Optional[int] = None  # set when kind == "clarification_reply"


# =============================================================================
# Long-lived per-channel session
# =============================================================================


class LongLivedSession:
    """In-memory agent session for one Discord channel.

    Owns: a serialising ``asyncio.Lock``, a bounded inbox queue, the
    three-state pending-prompt marker, and (later) the in-flight agent
    task.

    The three-state ``_pending_prompt_message_id`` works as follows:

    - ``None`` — no clarification outstanding.  ``deliver_message``
      enqueues into ``_inbox`` as a plain ``"message"``.
    - :data:`SENTINEL_PENDING` — agent has requested a clarification
      but the prompt has not yet been posted.  ``deliver_message``
      buffers the event into ``_inbox`` as a plain ``"message"`` —
      it must NOT be consumed as the answer because the user hasn't
      seen the prompt yet.
    - concrete ``int`` — the prompt's discord message id.
      ``deliver_message`` admits exactly one event as the
      clarification answer (``kind="clarification_reply"``) and
      atomically transitions back to ``None``.  Subsequent events
      go into the plain inbox until the next prompt.
    """

    def __init__(
        self,
        session_key: str,
        *,
        scope_id: Optional[str] = None,
        bot_user_id: Optional[int] = None,
        inbox_maxsize: int = DEFAULT_INBOX_MAXSIZE,
    ) -> None:
        self.session_key = session_key
        self.scope_id = scope_id
        self.bot_user_id = bot_user_id
        self._lock = asyncio.Lock()
        self._inbox: asyncio.Queue[InboundMessage] = asyncio.Queue(
            maxsize=inbox_maxsize
        )
        self._pending_prompt_message_id: Any = None
        # Set by P6+ when the agent loop spawns; P5 leaves it None so
        # tests / introspection can observe "no worker yet".
        self._in_flight: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_bot_user_id(self, bot_user_id: Optional[int]) -> None:
        """Late-bind the bot's user id (captured by adapter ``on_ready``)."""
        self.bot_user_id = bot_user_id

    # ------------------------------------------------------------------
    # Pending-prompt state machine (called by P6+ agent loop)
    # ------------------------------------------------------------------

    async def mark_prompt_pending(self) -> None:
        """Agent has decided to ask a clarification but hasn't posted yet."""
        async with self._lock:
            self._pending_prompt_message_id = SENTINEL_PENDING

    async def mark_prompt_posted(self, message_id: int) -> None:
        """Prompt is now live on Discord; transition SENTINEL → ``message_id``.

        Raises ``RuntimeError`` if called from any other state — this
        catches misuse from the future agent loop where the sentinel
        was never set or has already been resolved/cleared.
        """
        if not isinstance(message_id, int):
            raise TypeError(
                f"message_id must be int, got {type(message_id).__name__}"
            )
        async with self._lock:
            if self._pending_prompt_message_id is not SENTINEL_PENDING:
                raise RuntimeError(
                    "mark_prompt_posted requires SENTINEL_PENDING state; "
                    f"current state={self._pending_prompt_message_id!r}"
                )
            self._pending_prompt_message_id = message_id

    async def clear_pending(self) -> None:
        """Reset to ``None`` (used on timeout / cancel)."""
        async with self._lock:
            self._pending_prompt_message_id = None

    # ------------------------------------------------------------------
    # Introspection (read-only, no lock — best-effort snapshot)
    # ------------------------------------------------------------------

    @property
    def pending_prompt_state(self) -> Any:
        """Snapshot of the pending-prompt marker; for tests/diagnostics."""
        return self._pending_prompt_message_id

    def inbox_qsize(self) -> int:
        return self._inbox.qsize()

    # ------------------------------------------------------------------
    # Gateway-side delivery
    # ------------------------------------------------------------------

    async def deliver_message(self, event: Any) -> bool:
        """Hand a gateway ``MessageEvent`` to this session.

        Returns ``True`` if the event was accepted (queued or admitted
        as a clarification answer), ``False`` if dropped (bot self-message
        or inbox full).

        Serialises under ``self._lock`` so the pending-state transition
        and the inbox enqueue happen as one atomic step — without this,
        two concurrent deliveries during an open clarification could
        both observe ``pending == 100`` and both be admitted.
        """
        author_id = self._extract_author_id(event)

        async with self._lock:
            # 1. Drop self-messages.  We check inside the lock so a late
            #    bot_user_id late-bind from on_ready can't race with a
            #    delivery that started reading None.
            if (
                self.bot_user_id is not None
                and author_id is not None
                and int(author_id) == int(self.bot_user_id)
            ):
                return False

            # 2. Concrete pending → admit exactly one as the answer.
            current = self._pending_prompt_message_id
            if isinstance(current, int):
                payload = InboundMessage(
                    kind="clarification_reply",
                    event=event,
                    prompt_message_id=current,
                )
                # Try the enqueue FIRST.  If the inbox is full we must
                # NOT clear the pending state — otherwise the session
                # silently forgets it still owes a clarification answer
                # while the user's reply has been dropped.
                if self._enqueue(payload):
                    self._pending_prompt_message_id = None
                    return True
                return False

            # 3. None or SENTINEL_PENDING → plain message.
            payload = InboundMessage(kind="message", event=event)
            return self._enqueue(payload)

    # ------------------------------------------------------------------
    # Agent-side consumption
    # ------------------------------------------------------------------

    async def get_next(self) -> InboundMessage:
        """Block until the next inbound is available (used by P6+ loop)."""
        return await self._inbox.get()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_author_id(event: Any) -> Optional[int]:
        """Pull the author id off a gateway event, regardless of shape.

        Works for the real ``MessageEvent`` (``event.source.user_id``)
        and for test fakes that expose ``event.author_id`` directly.
        Returns ``None`` if neither is present, which intentionally
        prevents the self-drop path from firing on malformed events.
        """
        author_id = getattr(event, "author_id", None)
        if author_id is not None:
            return author_id
        source = getattr(event, "source", None)
        if source is not None:
            return getattr(source, "user_id", None)
        return None

    def _enqueue(self, payload: InboundMessage) -> bool:
        """Non-blocking put; logs and drops on full queue.

        Called under ``self._lock`` — must not await.
        """
        try:
            self._inbox.put_nowait(payload)
            return True
        except asyncio.QueueFull:
            logger.warning(
                "LongLivedSession[%s] inbox full (size=%d); dropping %s",
                self.session_key,
                self._inbox.maxsize,
                payload.kind,
            )
            return False


# =============================================================================
# Router: per-runner registry of LongLivedSession by session_key
# =============================================================================


class SessionRouter:
    """Registry of long-lived sessions keyed by gateway ``session_key``.

    Owned by :class:`gateway.run.GatewayRunner`.  ``get`` is the
    fast-path lookup the runner does for every inbound message before
    falling through to the per-message agent path.

    P5 only constructs sessions in tests; production registration of
    channels into this router lands in P6 when the orchestrator picks
    up category creation.
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, LongLivedSession] = {}

    def register(self, session_key: str, session: LongLivedSession) -> None:
        """Register *session* under *session_key*.

        Re-registering the same key replaces the prior session — the
        caller (P6 orchestrator) is responsible for tearing the prior
        session down first if needed.
        """
        self._sessions[session_key] = session

    def unregister(self, session_key: str) -> Optional[LongLivedSession]:
        """Remove and return the session at *session_key*, or ``None``."""
        return self._sessions.pop(session_key, None)

    def get(self, session_key: str) -> Optional[LongLivedSession]:
        """Return the session at *session_key* or ``None`` (fast-path)."""
        return self._sessions.get(session_key)

    def __contains__(self, session_key: str) -> bool:
        return session_key in self._sessions

    def __len__(self) -> int:
        return len(self._sessions)
