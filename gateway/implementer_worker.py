"""Implementer worker (P7a-2).

Each per-stream :class:`gateway.session_router.LongLivedSession` gets one
of these workers spawned by ``gateway.stream_bootstrap`` after the
operator approves the orchestrator's plan.

P7a-2 wires this end-to-end:

1. Loads the role-specific persona prompt (``implementer_frontend`` or
   ``implementer_backend``) on startup.
2. Lazily constructs a real :class:`run_agent.AIAgent` on the first
   inbound message, with model/provider routing pulled from
   ``GatewayRunner._resolve_implementer_agent_config(role)``.
3. Builds a :class:`tools.sandboxed_toolset.SandboxedToolset` rooted at
   the stream's worktree, and binds it onto the per-turn
   ``ToolDispatchContext`` so file/terminal tools are gated through the
   sandbox (path arguments confined to the worktree, terminal
   ``workdir`` forced to the worktree root).
4. Loops the inbox, runs each turn on a worker thread via
   ``asyncio.to_thread``, and persists conversation history on the
   local stack so the agent sees its own prior turns.
5. Updates ``runstate.json`` per turn (``status="running"``,
   ``turn_count`` bumped). Terminal turn errors set ``status="error"``
   plus ``reason`` and surface a recoverable message to the stream
   channel — the worker keeps consuming, no infinite retry.

The public surface (``async def implementer_worker(session, *, runner,
adapter)``) and lifecycle contract are intentionally identical to the
P7a-1 stub — bootstrap only needs an import swap.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from gateway.personas import (
    IMPLEMENTER_BACKEND,
    load_persona_system_prompt,
    role_to_persona,
)
from hermes_cli.runstate import write_stream_runstate
from tools.registry import (
    ToolDispatchContext,
    use_dispatch_context,
)
from tools.sandboxed_toolset import SandboxedToolset, build_stream_sandbox

if TYPE_CHECKING:  # pragma: no cover - import-cycle protection
    from gateway.run import GatewayRunner
    from gateway.session_router import LongLivedSession

logger = logging.getLogger(__name__)


# Toolsets the implementer persona is allowed to use.  The sandbox layer
# additionally gates ``file`` + ``terminal`` calls (path arguments must
# resolve under the per-stream worktree); workflow + discord tools pass
# through unchanged because they don't carry filesystem paths.
#
# ``discord-orchestration-admin`` and ``workflow-orchestrator-mutating``
# are deliberately omitted — implementer agents must not create channels
# or approve plans.
IMPLEMENTER_TOOLSETS: List[str] = [
    "file",
    "terminal",
    "workflow-stream-mutating",
    "workflow-readonly",
    "discord-orchestration-stream",
]


def _extract_text(event: Any) -> str:
    """Pull the user's text off an inbound gateway event.

    ``InboundMessage.event`` is opaque (a real ``MessageEvent`` in
    production, a fake in tests); ``getattr`` keeps this module decoupled
    from ``gateway.platforms.base``.
    """
    text = getattr(event, "text", None)
    if isinstance(text, str):
        return text
    return ""


def _construct_agent_for_session(
    session: "LongLivedSession",
    system_prompt: str,
    runner: "GatewayRunner",
):
    """Construct the per-stream AIAgent.

    Lazy-imported because :mod:`run_agent` pulls in the full
    provider/credential stack — we don't want to pay that cost for
    streams that never get a message.

    Model + provider/credentials come from
    :meth:`GatewayRunner._resolve_implementer_agent_config` so frontend
    vs backend implementers route to the configured upstream model.
    """
    from run_agent import AIAgent

    role = session.role or "backend"
    cfg = runner._resolve_implementer_agent_config(role)

    max_iterations = (
        runner.config.max_iterations
        if hasattr(runner.config, "max_iterations") and runner.config.max_iterations
        else 90
    )

    # AIAgent rejects ``None`` for some kwargs (e.g. ``api_key`` when no
    # ``provider`` is set). Drop ``None`` values so the constructor's
    # own defaults apply.
    kwargs = {
        "model": cfg.get("model"),
        "max_iterations": max_iterations,
        "quiet_mode": True,
        "verbose_logging": False,
        "enabled_toolsets": IMPLEMENTER_TOOLSETS,
        "ephemeral_system_prompt": system_prompt,
        "session_id": session.session_key,
        "platform": "discord",
        "session_db": getattr(runner, "_session_db", None),
    }
    for opt in ("provider", "base_url", "api_key", "api_mode", "fallback_model"):
        val = cfg.get(opt)
        if val is not None:
            kwargs[opt] = val
    return AIAgent(**kwargs)


def _build_stream_sandbox(session: "LongLivedSession") -> Optional[SandboxedToolset]:
    """Construct the sandbox rooted at this stream's worktree.

    Returns ``None`` when ``session.worktree_root`` is missing — that is
    a bootstrap bug, not a normal state, but treating it as "no sandbox"
    is preferable to crashing the worker. The downstream registry path
    sees ``ctx.sandbox is None`` and dispatches normally.
    """
    root = session.worktree_root
    if root is None:
        logger.warning(
            "implementer_worker: session %s has no worktree_root; "
            "running WITHOUT sandbox (bootstrap bug)",
            session.session_key,
        )
        return None
    try:
        # ``registry`` here is the global ToolRegistry — sandbox.dispatch
        # calls back into it after rewriting paths, with the recursion
        # guard ContextVar in registry.py preventing re-entry.
        from tools.registry import registry as _global_registry

        return build_stream_sandbox(root, registry=_global_registry)
    except Exception:
        logger.exception(
            "implementer_worker: failed to build sandbox for %s root=%s",
            session.session_key, root,
        )
        return None


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
            "implementer_worker: failed to post to %s for session %s",
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
        # be persisted. Should never happen but if it does, skip.
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
            "implementer_worker: failed to write stream runstate for %s",
            session.session_key,
            exc_info=True,
        )


async def implementer_worker(
    session: "LongLivedSession",
    *,
    runner: "GatewayRunner",
    adapter: Any,
) -> None:
    """Long-lived consumer of ``session._inbox``.

    Lifecycle: spawned by
    ``gateway.stream_bootstrap.bootstrap_streams_for_project``; ends only
    when its task is cancelled (router teardown / project cleanup) or
    the persona prompt fails to load on startup. Per-turn errors are
    swallowed (logged + posted to the stream channel) so a single bad
    inbound can't kill the worker.

    ``conversation_history`` lives on the local stack — it's lost on
    process restart, which is consistent with P5/P6/P7a's "in-memory
    only" charter (rehydration is a P7b+ concern).
    """
    # Persona resolution.  ``session.persona`` is set by stream_bootstrap
    # via ``role_to_persona(role)``; fall back to deriving from
    # ``session.role`` when persona is missing (defensive: lets us swap
    # the bootstrap path without coordinated changes).
    persona = session.persona
    if not persona:
        try:
            persona = role_to_persona(session.role or "")
        except ValueError:
            logger.warning(
                "implementer_worker: session %s has no persona and "
                "role=%r is unknown; defaulting to %s",
                session.session_key, session.role, IMPLEMENTER_BACKEND,
            )
            persona = IMPLEMENTER_BACKEND

    try:
        system_prompt = load_persona_system_prompt(persona)
    except Exception:
        logger.exception(
            "implementer_worker: failed to load persona prompt for %s persona=%s",
            session.session_key, persona,
        )
        await _post_to_stream_channel(
            session, adapter,
            f"⚠️ Failed to load `{persona}` persona prompt — implementer "
            "session aborted. Operator: re-run plan approval after fixing "
            "the prompt path.",
        )
        # Mark runstate as errored so observability sees the abort.
        _safe_write_stream_runstate(
            session,
            status="error",
            reason=f"failed to load persona prompt {persona!r}",
        )
        return

    # Build the sandbox once at startup.  Sandbox root is the stream's
    # worktree; if missing (bootstrap bug) we run without one and rely
    # on persona-prompt discipline.
    sandbox = _build_stream_sandbox(session)

    history: List[Dict[str, Any]] = []
    agent = None  # lazy-construct on first inbound
    turn_count = 0

    def _build_dispatch_ctx() -> ToolDispatchContext:
        # Re-built each turn so a future field that varies per turn
        # picks up; today everything is stable for the session lifetime.
        return ToolDispatchContext(
            scope_id=session.scope_id,
            slug=session.slug,
            stream_name=session.stream_name,
            channel_id=session.main_channel_id,
            worktree_root=session.worktree_root,
            sandbox=sandbox,
            # Lambda so the dispatcher sees the *current* closed flag —
            # not whatever value it had when the turn started.  Teardown
            # sets ``session.closed = True`` while the to-thread agent
            # may still be issuing tool calls; this gates them.
            closed_check=lambda: bool(getattr(session, "closed", False)),
        )

    while True:
        try:
            inbound = await session.get_next()
        except asyncio.CancelledError:
            logger.info(
                "implementer_worker cancelled for %s",
                session.session_key,
            )
            return

        try:
            text = _extract_text(inbound.event)
            if not text:
                # Stickers / reactions / empty events shouldn't bump the
                # turn count or fire the agent. The dispatch already
                # chose to deliver them; we just skip the work.
                logger.debug(
                    "implementer_worker[%s]: empty text on %s event; skipping",
                    session.session_key, inbound.kind,
                )
                continue

            if agent is None:
                agent = _construct_agent_for_session(session, system_prompt, runner)

            turn_count += 1
            # P7a-2 codex review (Suggestion): explicitly clear ``reason``
            # when transitioning back to ``running`` so a previous turn's
            # error reason doesn't linger in observability across recovery.
            _safe_write_stream_runstate(
                session,
                status="running",
                turn_count=turn_count,
                reason=None,
            )

            dispatch_ctx = _build_dispatch_ctx()

            def _run_turn():
                # Bind the ambient dispatch context for the duration of
                # this turn.  Done inside the to-thread'd function so
                # the contextvar is set on the worker thread that
                # actually invokes registry.dispatch().
                with use_dispatch_context(dispatch_ctx):
                    return agent.run_conversation(
                        text,
                        conversation_history=history,
                        task_id=session.session_key,
                    )

            result = await asyncio.to_thread(_run_turn)
            # P7a-2 codex review (Important #5): ``run_agent.AIAgent.run_conversation``
            # returns the updated conversation under ``"messages"``; the earlier
            # ``"conversation_history"`` key never existed and silently dropped
            # every prior turn. Read the correct key so the agent actually
            # sees its own history.
            new_history = (
                result.get("messages") if isinstance(result, dict) else None
            )
            if isinstance(new_history, list):
                history = new_history
        except asyncio.CancelledError:
            logger.info(
                "implementer_worker cancelled mid-turn for %s",
                session.session_key,
            )
            return
        except Exception as exc:
            logger.exception(
                "implementer_worker turn failed for %s",
                session.session_key,
            )
            _safe_write_stream_runstate(
                session,
                status="error",
                reason=str(exc),
            )
            await _post_to_stream_channel(
                session,
                adapter,
                "⚠️ The implementer agent encountered an error on this "
                "turn. The session is still live — send another message "
                "to retry.",
            )
