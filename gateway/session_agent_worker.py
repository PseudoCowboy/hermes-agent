"""Per-session async agent worker (P6).

Each :class:`gateway.session_router.LongLivedSession` has at most one
``session_agent_worker`` task running.  The worker:

1. Loads the persona's system prompt once at startup.
2. Lazily constructs an :class:`run_agent.AIAgent` on the first inbound
   message (so the AIAgent's heavyweight provider/credential resolution
   doesn't fire until we actually have work).
3. Loops ``await session.get_next()`` for inbound messages, bridges each
   one to a single ``agent.run_conversation(...)`` call run on a worker
   thread (``asyncio.to_thread``) so the event loop stays responsive
   for inbox deliveries and Discord reactions.
4. Persists ``conversation_history`` between turns so the orchestrator
   sees its own prior planning + the operator's full reply chain.
5. Soft-fails per turn — a bad agent turn surfaces an error message in
   the session's main channel but does NOT kill the worker; the operator
   can retry with another message.

P6 ships only the orchestrator persona end-to-end.  Implementer /
test-agent personas are stubbed in :mod:`gateway.personas` and will
land in P7.

Scope-binding:
    Before each turn, the worker enters
    ``tools.registry.use_dispatch_context(...)`` with a
    ``ToolDispatchContext`` carrying the session's ``scope_id`` and
    ``slug``.  Tool handlers that accept ``scope_id`` / ``slug`` /
    ``stream_name`` get those filled in automatically when the agent
    leaves them blank — explicit caller args still win, never silently
    overridden.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from gateway.personas import ORCHESTRATOR, load_persona_system_prompt
from tools.registry import ToolDispatchContext, use_dispatch_context

if TYPE_CHECKING:  # pragma: no cover - import-cycle protection
    from gateway.run import GatewayRunner
    from gateway.session_router import LongLivedSession

logger = logging.getLogger(__name__)


# Toolsets the orchestrator persona is allowed to use.  Kept narrow on
# purpose — orchestrator drafts plans and posts reactions, it never
# touches files, terminals, git, or per-stream channel admin.
#
# Note: ``discord-orchestration-admin`` (create/archive categories) is
# deliberately NOT included.  The orchestrator persona prompt forbids
# category mutation, but prompt-only restrictions aren't a security
# boundary — the toolset gate is.  Bootstrap (the only legitimate
# category-mutating actor in P6) is system code, not an agent tool call.
ORCHESTRATOR_TOOLSETS: List[str] = [
    "workflow-orchestrator-mutating",
    "discord-orchestration-stream",
]


# Mapping of persona → enabled toolsets.  P7 will add IMPLEMENTER and
# TEST_AGENT entries.  Keeping this co-located with the worker rather
# than in personas.py because the toolset selection is a worker concern
# (the persona module owns prompts, not capabilities).
PERSONA_TOOLSETS: Dict[str, List[str]] = {
    ORCHESTRATOR: ORCHESTRATOR_TOOLSETS,
}


def _extract_text(event: Any) -> str:
    """Pull the user's text off an inbound gateway event.

    ``InboundMessage.event`` is opaque (a real ``MessageEvent`` in
    production, a fake in tests); ``getattr`` keeps this module
    decoupled from ``gateway.platforms.base``.
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
    """Construct the per-session AIAgent.

    Lazy-imported because :mod:`run_agent` is a heavy import that pulls
    in the full provider/credential stack — we don't want to pay that
    cost for sessions that never get a message.

    Mirrors :mod:`gateway.run`'s per-message agent construction (model
    resolution, runtime kwargs, fallback) but with the persona's
    system prompt as ``ephemeral_system_prompt`` and a narrowed toolset
    matching the persona.
    """
    from run_agent import AIAgent

    persona = session.persona or ORCHESTRATOR
    enabled_toolsets = PERSONA_TOOLSETS.get(persona, ORCHESTRATOR_TOOLSETS)

    # Resolve model + runtime kwargs the same way the per-message
    # agent path does.  ``_resolve_turn_agent_config`` is the canonical
    # entrypoint and applies fallback model + provider routing rules.
    # We pass an empty message because the resolver only inspects the
    # config + runtime, not the message content.
    model = runner.config.model
    runtime_kwargs: Dict[str, Any] = {}
    try:
        turn_route = runner._resolve_turn_agent_config("", model, runtime_kwargs)
        model = turn_route.get("model", model)
        runtime_kwargs = turn_route.get("runtime", runtime_kwargs)
    except Exception:
        # _resolve_turn_agent_config is internal — fall back to bare
        # model resolution if its signature changes.  Worst case the
        # agent uses the global default.
        logger.debug("could not resolve turn agent config for %s; using bare model",
                     session.session_key, exc_info=True)

    return AIAgent(
        model=model,
        **runtime_kwargs,
        max_iterations=runner.config.max_iterations
        if hasattr(runner.config, "max_iterations") and runner.config.max_iterations
        else 90,
        quiet_mode=True,
        verbose_logging=False,
        enabled_toolsets=enabled_toolsets,
        ephemeral_system_prompt=system_prompt,
        session_id=session.session_key,
        platform="discord",
        session_db=getattr(runner, "_session_db", None),
        fallback_model=getattr(runner, "_fallback_model", None),
    )


async def _post_error_to_main_channel(
    session: "LongLivedSession",
    adapter: Any,
    message: str,
) -> None:
    """Best-effort post of an error message to the session's main channel.

    Swallows all exceptions — if we can't even post the error, the
    worker should keep running rather than crash.  The user can /reset
    or send another message to retry.

    Skips entirely when the session has been closed (teardown), so a
    late thread-side error from an already-cancelled turn doesn't post
    to a channel the operator just abandoned.
    """
    if getattr(session, "closed", False):
        return
    target = session.main_channel_id or session.session_key
    if not target:
        return
    try:
        await adapter.send(target, message)
    except Exception:
        logger.exception(
            "failed to post worker error to main channel for %s",
            session.session_key,
        )


async def session_agent_worker(
    session: "LongLivedSession",
    *,
    runner: "GatewayRunner",
    adapter: Any,
) -> None:
    """Long-lived consumer of ``session._inbox``.

    Lifecycle: spawned by ``gateway.project_bootstrap.bootstrap_new_project``;
    ends only when its task is cancelled (router teardown) or — never
    by an agent turn raising.  Each turn is wrapped in a try/except so
    a single bad turn surfaces an error to the operator and the worker
    keeps consuming.

    The worker holds NO module-global state.  ``conversation_history``
    lives on the local stack — it's lost on process restart, which is
    consistent with P5/P6's "in-memory only" charter (rehydration is a
    P7+ concern).
    """
    persona = session.persona or ORCHESTRATOR
    try:
        system_prompt = load_persona_system_prompt(persona)
    except Exception:
        logger.exception(
            "session_agent_worker: failed to load persona prompt for %s persona=%s",
            session.session_key, persona,
        )
        await _post_error_to_main_channel(
            session,
            adapter,
            f"⚠️ Failed to load `{persona}` persona prompt — session aborted.",
        )
        return

    history: List[Dict[str, Any]] = []
    agent = None  # lazy-construct on first turn
    # Track the human operator who can approve plans via reaction.
    # First non-empty user_id we see on the inbox wins — there is only
    # one operator per project channel (P6 invariant).
    approver_user_id: Optional[str] = None

    def _build_dispatch_ctx() -> ToolDispatchContext:
        # Re-built each turn so a newly observed approver_user_id picks
        # up; the rest of the fields are stable for the session lifetime.
        return ToolDispatchContext(
            scope_id=session.scope_id,
            slug=session.slug,
            stream_name=None,  # P7 binds streams; orchestrator has no stream.
            channel_id=session.main_channel_id,
            user_id=approver_user_id,
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
                "session_agent_worker cancelled for %s",
                session.session_key,
            )
            return

        try:
            if agent is None:
                agent = _construct_agent_for_session(session, system_prompt, runner)

            text = _extract_text(inbound.event)
            if not text:
                # Empty text events shouldn't reach the agent — they'd
                # waste an iteration and confuse the orchestrator's
                # clarification flow.  Silently skip; deliver_message
                # already chose to enqueue (e.g. user sent a sticker).
                logger.debug(
                    "session_agent_worker[%s]: empty text on %s event; skipping",
                    session.session_key, inbound.kind,
                )
                continue

            # Capture the operator's user_id the first time we see a
            # non-empty value — this is what discord_wait_for_reaction
            # will be auto-bound with so the orchestrator knows whose
            # ✅/❌ reaction to listen for.
            if approver_user_id is None:
                src = getattr(inbound.event, "source", None)
                src_user_id = getattr(src, "user_id", None) if src is not None else None
                if src_user_id:
                    approver_user_id = str(src_user_id)

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
            new_history = result.get("conversation_history") if isinstance(result, dict) else None
            if isinstance(new_history, list):
                history = new_history
        except asyncio.CancelledError:
            logger.info(
                "session_agent_worker cancelled mid-turn for %s",
                session.session_key,
            )
            return
        except Exception:
            logger.exception(
                "session_agent_worker turn failed for %s",
                session.session_key,
            )
            await _post_error_to_main_channel(
                session,
                adapter,
                "⚠️ The agent encountered an error on this turn. The session "
                "is still live — send another message to retry, or use "
                "/reset to start over.",
            )
