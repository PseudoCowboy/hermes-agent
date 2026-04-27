"""Stub implementer worker (P7a-1).

Each per-stream :class:`gateway.session_router.LongLivedSession` gets one
of these workers spawned by ``gateway.stream_bootstrap`` after the
operator approves the orchestrator's plan.

P7a-1 is a STUB: no AIAgent, no model routing, no sandbox dispatch.  The
worker exists only to:

1. Lock in the worker module's structure (lifecycle skeleton, inbox
   loop, ``closed_check``-aware shutdown) so P7a-2 can swap the body
   for the real ``AIAgent.run_conversation`` call without changing the
   surrounding plumbing.
2. Keep the per-stream channel responsive — operators sending messages
   into a stream channel during P7a-1 get a clear "not yet wired" reply
   instead of silence.
3. Update ``runstate.json`` per turn so that P7b's rehydration code has
   real on-disk state to test against.

The real implementer agent + ContextVar sandbox dispatch lands in P7a-2.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

from hermes_cli.runstate import write_stream_runstate

if TYPE_CHECKING:  # pragma: no cover - import-cycle protection
    from gateway.run import GatewayRunner
    from gateway.session_router import LongLivedSession

logger = logging.getLogger(__name__)


# Message posted into the stream channel on first startup.  Kept as a
# module constant so tests can assert on it without duplicating the
# string and going out of sync.
STARTUP_MESSAGE = (
    "Stream `{stream_name}` ready. "
    "Implementer agent will land in the next phase (P7a-2)."
)

REPLY_MESSAGE = (
    "Implementer agent not yet wired (P7a-2). Your message was logged."
)


def _extract_text(event: Any) -> str:
    text = getattr(event, "text", None)
    if isinstance(text, str):
        return text
    return ""


async def _post_to_stream_channel(
    session: "LongLivedSession",
    adapter: Any,
    message: str,
) -> None:
    """Best-effort post to the stream channel.

    Swallows all exceptions — network errors must not kill the worker.
    Skips entirely when the session has been closed (teardown), so a
    late post from a cancelled-but-not-yet-exited worker doesn't hit a
    channel the operator just abandoned.
    """
    if getattr(session, "closed", False):
        return
    target = session.main_channel_id
    if not target:
        return
    try:
        await adapter.send(target, message)
    except Exception:
        logger.exception(
            "implementer_stub_worker: failed to post to %s for session %s",
            target, session.session_key,
        )


def _safe_write_stream_runstate(
    session: "LongLivedSession",
    **fields_to_update: Any,
) -> None:
    """Write the stream runstate, swallowing exceptions.

    Runstate is observability — never fail the worker if disk IO breaks.
    Logged at warning level so the failure is visible without burying
    operator-relevant errors.
    """
    if not session.scope_id or not session.slug or not session.stream_name:
        # Defensive: a session without the bootstrap-set fields can't
        # be persisted.  Should never happen but if it does, skip.
        return
    try:
        write_stream_runstate(
            session.scope_id,
            session.slug,
            session.stream_name,
            **fields_to_update,
        )
    except Exception:
        logger.warning(
            "implementer_stub_worker: failed to write stream runstate for %s",
            session.session_key,
            exc_info=True,
        )


async def implementer_stub_worker(
    session: "LongLivedSession",
    *,
    runner: "GatewayRunner",
    adapter: Any,
) -> None:
    """Long-lived consumer of ``session._inbox`` — STUB version.

    Lifecycle: spawned by ``gateway.stream_bootstrap.bootstrap_streams_for_project``;
    ends only when its task is cancelled (router teardown / project
    cleanup).  Per-turn errors are swallowed so a single bad inbound
    can't kill the worker.

    Replaced wholesale by the real implementer agent in P7a-2.  The
    public surface (``async def implementer_stub_worker(session, *,
    runner, adapter)``) and lifecycle contract are stable across that
    swap so the bootstrap code doesn't need to change.
    """
    stream_name = session.stream_name or "<unknown>"

    # Post the startup banner into the stream channel.  Done before the
    # inbox loop so even a stream that never receives messages still
    # has visible proof-of-life.
    await _post_to_stream_channel(
        session,
        adapter,
        STARTUP_MESSAGE.format(stream_name=stream_name),
    )

    # Track turn count locally so we don't have to read the runstate
    # back to increment it.  Started at 0; each inbound bumps by one.
    turn_count = 0

    while True:
        try:
            inbound = await session.get_next()
        except asyncio.CancelledError:
            logger.info(
                "implementer_stub_worker cancelled for %s",
                session.session_key,
            )
            return

        try:
            text = _extract_text(inbound.event)
            if not text:
                # Stickers / reactions / empty events shouldn't bump the
                # turn count or post a stub reply.  The dispatch already
                # chose to deliver them; we just skip the work.
                logger.debug(
                    "implementer_stub_worker[%s]: empty text on %s event; skipping",
                    session.session_key, inbound.kind,
                )
                continue

            turn_count += 1
            _safe_write_stream_runstate(
                session,
                status="running",
                turn_count=turn_count,
            )
            await _post_to_stream_channel(session, adapter, REPLY_MESSAGE)
        except asyncio.CancelledError:
            logger.info(
                "implementer_stub_worker cancelled mid-turn for %s",
                session.session_key,
            )
            return
        except Exception:
            logger.exception(
                "implementer_stub_worker turn failed for %s",
                session.session_key,
            )
            # No user-facing post on internal errors — the stub doesn't
            # do anything load-bearing, surfacing internal failures
            # would just be noise.  Real worker (P7a-2) will surface
            # them, mirroring session_agent_worker.
