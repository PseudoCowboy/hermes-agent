"""Stream bootstrap (P7a-1).

Triggered by the orchestrator session worker after the operator approves
the orchestrator's plan (``workflow_approve_plan`` writes
``control/approval-event.json``; the worker reads-and-truncates it
post-turn and calls into here).

For each stream declared in the workstream manifest, we:

1. Validate ``agentRole ∈ {frontend, backend}`` (default ``backend``).
2. Materialise the per-stream git worktree.
3. Create the ``{slug}-{stream}`` text channel under the project's
   Discord category.
4. Suppress Discord auto-thread for that channel.
5. Build a per-stream :class:`gateway.session_router.LongLivedSession`
   keyed by the new channel, persona ``IMPLEMENTER``.
6. Write the per-stream ``runstate.json`` (status="awaiting-first-turn").
7. Spawn a *stub* implementer worker (P7a-1).  P7a-2 swaps the body
   for the real ``AIAgent.run_conversation`` driver.

Failure handling: any failure during step N rolls back streams 0..N-1
in reverse (cancel worker → unregister session → discard from
no_auto_thread → delete stream channel → remove worktree → delete
runstate file).  The integration worktree is **kept** (idempotent —
P7b's merge_queue depends on it surviving partial failures).

Concurrency: caller holds ``runner.merge_lock_for(scope_id, slug)`` for
the duration so two ✅ reactions on the same project can't double-
bootstrap.  P7b's merge_queue will share the same lock.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from gateway.implementer_worker import implementer_stub_worker
from gateway.personas import IMPLEMENTER
from gateway.platforms.discord_orchestration import (
    OrchestrationError,
    create_stream_channel,
)
from gateway.project_bootstrap import (
    _best_effort_delete_channel,
    _resolve_category,
    _resolve_guild,
)
from gateway.session import Platform, SessionSource
from gateway.session_router import LongLivedSession
from hermes_cli.project_worktree import (
    WorktreeError,
    ensure_integration_worktree,
    ensure_stream_worktree,
    remove_worktree,
    stream_branch_name,
)
from hermes_cli.runstate import write_project_runstate, write_stream_runstate

if TYPE_CHECKING:  # pragma: no cover - import-cycle protection
    from gateway.run import GatewayRunner

logger = logging.getLogger(__name__)


# Allowed implementer agent roles.  Drives model routing in P7a-2 (frontend
# → Gemini, backend → Claude).  Validated at bootstrap so a typo in the
# manifest fails loudly instead of silently routing to the wrong model.
_VALID_AGENT_ROLES = frozenset({"frontend", "backend"})
_DEFAULT_AGENT_ROLE = "backend"


# Inter-channel sleep to soften the Discord rate limit when a project
# bootstraps 5+ streams in quick succession.  Tested-against bound; do
# not lower without re-checking the global API rate limit headers.
_INTER_STREAM_SLEEP_S = 0.25


# -----------------------------------------------------------------------------
# Result dataclass
# -----------------------------------------------------------------------------


@dataclass
class StreamBootstrapEntry:
    """One bootstrapped stream — what the operator can see / inspect."""

    stream_name: str
    role: str
    channel_id: str
    session_key: str
    worktree_root: Path


@dataclass
class StreamBootstrapResult:
    """Returned by :func:`bootstrap_streams_for_project`.

    On success: ``error`` is ``None`` and ``streams`` lists every
    bootstrapped stream.  On failure: ``error`` is a human-readable
    string and ``streams`` is empty (full rollback already happened).
    """

    streams: List[StreamBootstrapEntry] = field(default_factory=list)
    error: Optional[str] = None
    summary_message: Optional[str] = None


# -----------------------------------------------------------------------------
# Manifest validation
# -----------------------------------------------------------------------------


def _normalise_manifest(
    workstream_manifest: Dict[str, Dict[str, Any]],
) -> List[Tuple[str, str]]:
    """Validate manifest and return ``[(stream_name, role), ...]``.

    *workstream_manifest* is the dict from
    ``workstreams/manifest.json`` keyed by stream name.  Each entry
    may contain an ``agentRole`` field (optional, defaults to
    ``"backend"``).

    Raises ``ValueError`` for invalid stream names or unknown roles —
    bootstrap aborts before any side effects so there's nothing to
    roll back.
    """
    if not isinstance(workstream_manifest, dict) or not workstream_manifest:
        raise ValueError("workstream_manifest must be a non-empty dict")

    out: List[Tuple[str, str]] = []
    for stream_name, entry in workstream_manifest.items():
        if not isinstance(stream_name, str) or not stream_name:
            raise ValueError(f"invalid stream name: {stream_name!r}")
        # Stream names map to filesystem dirs and Discord channel
        # suffixes; the same restriction as the workflow validator.
        if "/" in stream_name or "\\" in stream_name or stream_name in {".", ".."}:
            raise ValueError(f"invalid stream name: {stream_name!r}")

        role = _DEFAULT_AGENT_ROLE
        if isinstance(entry, dict):
            raw_role = entry.get("agentRole", _DEFAULT_AGENT_ROLE)
            if not isinstance(raw_role, str):
                raise ValueError(
                    f"stream {stream_name!r}: agentRole must be a string, "
                    f"got {type(raw_role).__name__}"
                )
            role = raw_role
        if role not in _VALID_AGENT_ROLES:
            raise ValueError(
                f"stream {stream_name!r}: invalid agentRole {role!r}; "
                f"expected one of {sorted(_VALID_AGENT_ROLES)}"
            )
        out.append((stream_name, role))
    return out


# -----------------------------------------------------------------------------
# Per-stream rollback
# -----------------------------------------------------------------------------


async def _rollback_stream(
    *,
    runner: "GatewayRunner",
    adapter: Any,
    entry: StreamBootstrapEntry,
    session: LongLivedSession,
    worker_task: Optional[asyncio.Task],
    scope_id: str,
    slug: str,
) -> None:
    """Tear down one stream that was created but a later step failed.

    Best-effort: every cleanup step is wrapped so a failing rollback
    can't mask the original error.  Order is the reverse of creation:
    cancel worker → unregister session → discard from no_auto_thread →
    delete channel → remove worktree → delete runstate file.
    """
    # 1. Cancel worker.
    if worker_task is not None and not worker_task.done():
        worker_task.cancel()
        try:
            await worker_task
        except (asyncio.CancelledError, Exception):
            pass

    # 2. Unregister session.
    try:
        runner._session_router.unregister(entry.session_key)
    except Exception:
        logger.exception(
            "rollback: failed to unregister session %s", entry.session_key
        )

    # 3. Discard from no_auto_thread set.
    no_thread_set = getattr(adapter, "_no_auto_thread_channels", None)
    if isinstance(no_thread_set, set):
        no_thread_set.discard(entry.channel_id)

    # 4. Delete channel.  Resolve via adapter so we can call .delete().
    try:
        chan = await _resolve_category(adapter, entry.channel_id)
        if chan is not None:
            await _best_effort_delete_channel(chan)
    except Exception:
        logger.exception("rollback: failed to delete channel %s", entry.channel_id)

    # 5. Remove worktree.  Force=True because there should be no
    # uncommitted work yet (the implementer never ran), and
    # delete_branch=True so the per-stream branch doesn't accumulate.
    try:
        remove_worktree(
            entry.worktree_root, delete_branch=True, force=True
        )
    except WorktreeError:
        logger.exception(
            "rollback: failed to remove worktree %s", entry.worktree_root
        )
    except Exception:
        logger.exception("rollback: unexpected failure removing worktree")

    # 6. Delete runstate file (best-effort).
    try:
        from hermes_cli.runstate import _runstate_path_stream

        rs_path = _runstate_path_stream(scope_id, slug, entry.stream_name)
        if rs_path.is_file():
            rs_path.unlink()
    except Exception:
        logger.exception("rollback: failed to delete stream runstate")


# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------


async def bootstrap_streams_for_project(
    *,
    runner: "GatewayRunner",
    scope_id: str,
    slug: str,
    main_channel_id: str,
    workstream_manifest: Dict[str, Dict[str, Any]],
    approved_head_sha: Optional[str],
    adapter: Any,
    guild_id: str,
) -> StreamBootstrapResult:
    """Materialise stream channels + worktrees + stub workers.

    Acquires ``runner.merge_lock_for(scope_id, slug)`` for the entire
    transaction.  On failure, rolls back every stream created so far
    (in reverse order) and returns ``StreamBootstrapResult`` with a
    populated ``error`` field — caller posts that to the main channel.
    """
    # Manifest validation FIRST — outside the lock and before any side
    # effects so a typo'd agentRole returns immediately.
    try:
        normalised = _normalise_manifest(workstream_manifest)
    except ValueError as exc:
        return StreamBootstrapResult(error=f"invalid manifest: {exc}")

    lock = await runner.merge_lock_for(scope_id, slug)
    async with lock:
        return await _bootstrap_under_lock(
            runner=runner,
            scope_id=scope_id,
            slug=slug,
            main_channel_id=main_channel_id,
            normalised=normalised,
            approved_head_sha=approved_head_sha,
            adapter=adapter,
            guild_id=guild_id,
        )


async def _bootstrap_under_lock(
    *,
    runner: "GatewayRunner",
    scope_id: str,
    slug: str,
    main_channel_id: str,
    normalised: List[Tuple[str, str]],
    approved_head_sha: Optional[str],
    adapter: Any,
    guild_id: str,
) -> StreamBootstrapResult:
    """Body of bootstrap, executed under the per-project merge lock.

    Split out so the lock scope is obvious in the public function and
    the rollback path stays one indentation level shallower.
    """
    # Resolve the project's Discord category — every stream channel is
    # created inside it.  Tests use FakeAdapter where _client returns
    # the guild directly.
    guild = _resolve_guild(adapter, guild_id)
    if guild is None:
        return StreamBootstrapResult(
            error=f"bot not connected to guild {guild_id}"
        )
    category = await _resolve_category(adapter, scope_id)
    if category is None:
        return StreamBootstrapResult(
            error=f"could not resolve project category {scope_id}"
        )

    # Materialise the integration worktree before any per-stream work.
    # Idempotent — workflow_approve_plan already created it; this is
    # belt-and-braces for the case where bootstrap runs without an
    # approve_plan having gone through (e.g. recovery from runstate).
    try:
        ensure_integration_worktree(scope_id, slug, base="HEAD")
    except WorktreeError as exc:
        return StreamBootstrapResult(
            error=f"could not materialise integration worktree: {exc}"
        )

    bootstrapped: List[Tuple[StreamBootstrapEntry, LongLivedSession, asyncio.Task]] = []

    try:
        for idx, (stream_name, role) in enumerate(normalised):
            if idx > 0:
                # Soften the Discord rate limit — bursting 5+
                # create_text_channel calls in a row trips the global
                # bucket.  Sleep is per-iteration so the FIRST stream
                # bootstraps with no extra latency.
                await asyncio.sleep(_INTER_STREAM_SLEEP_S)

            # Track partial state for THIS stream so an exception
            # between sub-steps doesn't leak resources.  The outer
            # rollback handles only fully-bootstrapped prior streams
            # (those appended to ``bootstrapped``); anything created
            # for the in-progress stream below has to be cleaned up
            # by the inner handler before re-raising.
            partial_wt: Optional[Path] = None
            partial_channel_id: Optional[str] = None
            partial_session_key: Optional[str] = None
            try:
                # Step 1: per-stream worktree.  ensure_stream_worktree is
                # idempotent — re-running on a partially-bootstrapped
                # project is safe.
                wt_path = ensure_stream_worktree(scope_id, slug, stream_name)
                partial_wt = wt_path

                # Step 2: stream channel.
                channel_name = f"{slug}-{stream_name}"
                created = await create_stream_channel(
                    category=category,
                    name=channel_name,
                    topic=f"implementer:{role} — {stream_name}",
                    reason="hermes plan-approval",
                )
                channel_id = created.channel_id
                partial_channel_id = channel_id

                # Step 3: suppress auto-thread.
                no_thread_set = getattr(adapter, "_no_auto_thread_channels", None)
                if isinstance(no_thread_set, set):
                    no_thread_set.add(channel_id)

                # Step 4: build the per-stream session.  Channel-scoped key
                # mirrors project_bootstrap: drop user_id so all collaborators
                # in the stream channel share one worker.
                stream_source = SessionSource(
                    platform=Platform.DISCORD,
                    chat_id=channel_id,
                    chat_type="group",
                    user_id=None,
                    user_name=None,
                )
                session_key = runner._session_key_for_source(stream_source)
                session = LongLivedSession(
                    session_key,
                    scope_id=scope_id,
                    bot_user_id=getattr(adapter, "bot_user_id", None),
                )
                session.persona = IMPLEMENTER
                session.slug = slug
                session.main_channel_id = channel_id  # the stream's own channel
                session.stream_name = stream_name
                session.worktree_root = wt_path
                session.role = role
                runner._session_router.register(session_key, session)
                partial_session_key = session_key

                # Step 5: write per-stream runstate.
                try:
                    write_stream_runstate(
                        scope_id, slug, stream_name,
                        status="awaiting-first-turn",
                        approved_head_sha=approved_head_sha,
                        channel_id=channel_id,
                    )
                except Exception:
                    # Runstate is observability — never fail bootstrap on
                    # an IO error here.  The session is already registered.
                    logger.warning(
                        "failed to write initial stream runstate for %s/%s",
                        slug, stream_name, exc_info=True,
                    )

                # Step 6: spawn the stub worker.
                worker_task = asyncio.create_task(
                    implementer_stub_worker(
                        session, runner=runner, adapter=adapter
                    ),
                    name=f"impl-stub:{session_key}",
                )
                session._in_flight = worker_task

                entry = StreamBootstrapEntry(
                    stream_name=stream_name,
                    role=role,
                    channel_id=channel_id,
                    session_key=session_key,
                    worktree_root=wt_path,
                )
                bootstrapped.append((entry, session, worker_task))
            except Exception:
                # Per-stream cleanup for partial state created BEFORE
                # the entry was appended to ``bootstrapped``.  Without
                # this, a failure between worktree creation and channel
                # creation (or any other intermediate step) would leak
                # the worktree / branch / channel / session.  Each step
                # is best-effort so a stuck cleanup can't shadow the
                # original error which we re-raise to the outer handler.
                if partial_session_key is not None:
                    try:
                        runner._session_router.unregister(partial_session_key)
                    except Exception:
                        logger.exception(
                            "partial-rollback: failed to unregister session %s",
                            partial_session_key,
                        )
                if partial_channel_id is not None:
                    no_thread_set = getattr(
                        adapter, "_no_auto_thread_channels", None,
                    )
                    if isinstance(no_thread_set, set):
                        no_thread_set.discard(partial_channel_id)
                    try:
                        chan = await _resolve_category(
                            adapter, partial_channel_id,
                        )
                        if chan is not None:
                            await _best_effort_delete_channel(chan)
                    except Exception:
                        logger.exception(
                            "partial-rollback: failed to delete channel %s",
                            partial_channel_id,
                        )
                if partial_wt is not None:
                    try:
                        remove_worktree(
                            partial_wt, delete_branch=True, force=True,
                        )
                    except WorktreeError:
                        logger.exception(
                            "partial-rollback: failed to remove worktree %s",
                            partial_wt,
                        )
                    except Exception:
                        logger.exception(
                            "partial-rollback: unexpected failure removing worktree"
                        )
                    # Best-effort delete the partial runstate file too.
                    try:
                        from hermes_cli.runstate import _runstate_path_stream

                        rs_path = _runstate_path_stream(
                            scope_id, slug, stream_name,
                        )
                        if rs_path.is_file():
                            rs_path.unlink()
                    except Exception:
                        logger.exception(
                            "partial-rollback: failed to delete runstate"
                        )
                raise

    except Exception as exc:
        logger.exception("stream bootstrap failed for %s/%s", scope_id, slug)
        # Rollback in reverse.
        for entry, session, worker_task in reversed(bootstrapped):
            await _rollback_stream(
                runner=runner,
                adapter=adapter,
                entry=entry,
                session=session,
                worker_task=worker_task,
                scope_id=scope_id,
                slug=slug,
            )
        return StreamBootstrapResult(error=f"stream bootstrap failed: {exc}")

    # Step 7: write project runstate phase=streams-active.
    try:
        write_project_runstate(
            scope_id, slug,
            phase="streams-active",
            merged_streams=[],
            approved_head_sha=approved_head_sha,
            main_channel_id=main_channel_id,
        )
    except Exception:
        # Same rationale as per-stream runstate — observability only.
        logger.warning(
            "failed to write project runstate for %s/%s",
            scope_id, slug, exc_info=True,
        )

    # Step 8: post a single summary message to the main channel.
    streams = [b[0] for b in bootstrapped]
    summary = _format_summary_message(streams)
    try:
        await adapter.send(main_channel_id, summary)
    except Exception:
        logger.exception(
            "failed to post stream-bootstrap summary to main channel %s",
            main_channel_id,
        )

    return StreamBootstrapResult(streams=streams, summary_message=summary)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _format_summary_message(streams: List[StreamBootstrapEntry]) -> str:
    """Compose the main-channel summary post.

    Lists each new stream channel as a Discord channel mention so the
    operator can click through.  The "implementer not yet wired" note
    is important — without it the operator would expect the stub workers
    to actually do work in P7a-1.
    """
    if not streams:
        return "✓ Plan approved — no streams to bootstrap."
    lines = ["✓ Plan approved — streams ready:"]
    for s in streams:
        lines.append(f"• `{s.stream_name}` ({s.role}) → <#{s.channel_id}>")
    lines.append(
        "_Implementer agents are not yet wired (P7a-1 stubs only). "
        "Real agents land in the next phase._"
    )
    return "\n".join(lines)
