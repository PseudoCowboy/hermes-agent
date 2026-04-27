"""Project bootstrap (`!new`) (P6).

The home-channel pre-router in :mod:`gateway.run` calls into here when
the operator types ``!new <requirement>`` in the configured Discord
home channel.  Bootstrap creates one project end-to-end:

1. Slugify the requirement → human-readable project id.
2. Create the Discord category (the project's ``scope_id``).
3. Create the ``{slug}-main`` text channel inside the category — this
   is the orchestrator's main channel.
4. Build the :class:`gateway.session_router.LongLivedSession` keyed by
   the new channel, mark its persona as ``"orchestrator"`` and bind
   ``scope_id`` / ``slug`` / ``main_channel_id`` so the worker can
   thread them into tool dispatch contexts later.
5. Register the session on the runner's ``SessionRouter`` so the P5
   fast-path picks up subsequent operator messages in this channel.
6. Spawn :func:`gateway.session_agent_worker.session_agent_worker`
   as a long-lived asyncio task on the session.
7. Seed the inbox with a synthetic ``MessageEvent`` carrying the
   original requirement text so the orchestrator's first turn fires
   immediately (re-using ``deliver_message`` keeps the bounded queue
   + admission machinery uniform).

Failure handling: any step from #2 onward that raises triggers a
best-effort rollback (delete category, unregister session, cancel
worker) and returns an error string for the home channel.  Bootstrap
is one transaction from the operator's POV — no half-created projects.

Out of scope (P7+):
    Stream channel creation after plan approval, implementer worker
    spawn, archival flow on plan-rejection.  P6 only constructs the
    orchestrator side.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, Optional

from gateway.personas import ORCHESTRATOR
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.discord_orchestration import (
    OrchestrationError,
    create_project_category,
    create_stream_channel,
)
from gateway.session import Platform, SessionSource, build_session_key
from gateway.session_agent_worker import session_agent_worker
from gateway.session_router import LongLivedSession

if TYPE_CHECKING:  # pragma: no cover - import-cycle protection
    from gateway.run import GatewayRunner

logger = logging.getLogger(__name__)


# Discord category names: 1-100 chars; we cap slugs harder to keep the
# main channel name short and to leave headroom for stream channel
# suffixes that P7 will append (e.g. ``{slug}-frontend``).
SLUG_MAX_LEN = 32
SLUG_FALLBACK = "project"


def _slugify(text: str) -> str:
    """Lowercase + dash-separated slug, truncated to ``SLUG_MAX_LEN``.

    Conservative on purpose — Discord channel names allow only
    ``[a-z0-9_-]`` and the orchestration validator tightens that
    further.  Empty / unicode-only input falls back to ``"project"``
    rather than raising; the operator can rename later.
    """
    if not isinstance(text, str):
        return SLUG_FALLBACK
    # Replace any run of non-alphanumeric with a single dash, then
    # collapse leading/trailing dashes.
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    if not cleaned:
        return SLUG_FALLBACK
    if len(cleaned) > SLUG_MAX_LEN:
        cleaned = cleaned[:SLUG_MAX_LEN].rstrip("-")
        if not cleaned:
            return SLUG_FALLBACK
    return cleaned


def _synthesize_initial_event(
    source: SessionSource,
    requirement: str,
) -> MessageEvent:
    """Wrap the ``!new`` requirement as a synthetic MessageEvent.

    Marked ``internal=True`` so any downstream auth / rate-limit
    middleware that distinguishes user vs system messages doesn't
    re-validate it (the operator already cleared the home-channel gate).
    """
    return MessageEvent(
        text=requirement,
        message_type=MessageType.TEXT,
        source=source,
        internal=True,
    )


def _resolve_guild(adapter: Any, guild_id: str) -> Optional[Any]:
    """Look up the guild object on the discord client.

    Returns ``None`` if the bot isn't connected to the configured guild
    — the bootstrap caller surfaces that as an error rather than crash.
    """
    client = getattr(adapter, "_client", None)
    if client is None:
        return None
    try:
        gid = int(guild_id)
    except (TypeError, ValueError):
        return None
    return client.get_guild(gid)


async def _resolve_category(adapter: Any, category_id: str) -> Optional[Any]:
    """Fetch the category object so we can call ``create_text_channel``.

    ``create_stream_channel`` takes a category *object* (it calls
    ``category.create_text_channel(...)``), not just an id, so after
    creating a category by id we have to look the object back up.
    """
    client = getattr(adapter, "_client", None)
    if client is None:
        return None
    try:
        cid = int(category_id)
    except (TypeError, ValueError):
        return None
    chan = client.get_channel(cid)
    if chan is not None:
        return chan
    try:
        return await client.fetch_channel(cid)
    except Exception:
        return None


async def _best_effort_delete_channel(channel: Any) -> None:
    """Delete a freshly-created channel during rollback.

    Bare ``category.delete`` does NOT cascade across all Discord
    backends; explicitly deleting the channel first guarantees we
    don't leave an orphaned text channel behind when the category
    refuses to delete because it still has children.
    """
    if channel is None:
        return
    try:
        await channel.delete(reason="hermes bootstrap rollback")
    except Exception:
        logger.exception("rollback: failed to delete channel")


async def _best_effort_resolve_and_delete_category_by_id(
    adapter: Any, category_id: Optional[str]
) -> None:
    """Resolve a category by id and delete it during rollback.

    Used when ``_resolve_category`` originally failed (so we never had
    the object) but we still know the id from
    ``create_project_category``'s return value.  Best-effort: a missing
    id is a no-op.
    """
    if not category_id:
        return
    cat = await _resolve_category(adapter, category_id)
    if cat is None:
        return
    await _best_effort_delete_category(cat)


async def _best_effort_delete_category(category: Any) -> None:
    """Tear down a half-created project category during rollback.

    Swallows all exceptions — rollback runs on the failure path; we
    never want the cleanup itself to mask the original error.
    """
    if category is None:
        return
    try:
        await category.delete(reason="hermes bootstrap rollback")
    except Exception:
        logger.exception("rollback: failed to delete category")


async def bootstrap_new_project(
    *,
    runner: "GatewayRunner",
    requirement: str,
    source: SessionSource,
    adapter: Any,
    guild_id: str,
) -> str:
    """Create a project category + main channel + orchestrator worker.

    Returns a user-facing string for the home-channel reply.  On
    failure, returns an error string and leaves the gateway in a
    consistent state (no half-created sessions, no orphaned channels).

    *guild_id* is the Discord guild id from
    :attr:`PlatformConfig.orchestration_guild_id`.  *adapter* is the
    Discord adapter; we use ``adapter._client`` to resolve guild and
    channel objects.  The reason we take *adapter* directly rather
    than re-look-up via ``runner.adapters[Platform.DISCORD]`` is so
    tests can pass a fake adapter without registering it on the runner.
    """
    requirement = (requirement or "").strip()
    if not requirement:
        return (
            "Usage: `!new <requirement>` — describe the work you want "
            "the system to take on."
        )

    slug = _slugify(requirement)

    guild = _resolve_guild(adapter, guild_id)
    if guild is None:
        return (
            f"⚠️ Bootstrap failed — bot is not connected to guild "
            f"`{guild_id}`. Check your gateway.discord.orchestration_guild_id "
            f"config."
        )

    category = None
    main_channel = None  # raw channel object so rollback can delete it directly
    created_category_id: Optional[str] = None  # for delete-by-id when _resolve_category fails
    created_main_channel_id: Optional[str] = None  # ditto for the main channel
    session_key: Optional[str] = None
    worker_task: Optional[asyncio.Task] = None
    try:
        # Step 1: create the project category.
        created_category = await create_project_category(
            guild=guild,
            name=slug,
            reason=f"hermes !new: {requirement[:80]}",
        )
        scope_id = created_category.category_id
        created_category_id = scope_id

        # Re-fetch the category object (we only have its id from the
        # wrapper return value, but create_stream_channel needs the
        # object so it can call create_text_channel on it).
        category = await _resolve_category(adapter, scope_id)
        if category is None:
            raise OrchestrationError(
                f"could not resolve newly-created category {scope_id}"
            )

        # Step 2: create the {slug}-main channel.
        created_channel = await create_stream_channel(
            category=category,
            name=f"{slug}-main",
            topic=f"orchestrator main channel for {slug}",
            reason="hermes !new",
        )
        main_channel_id = created_channel.channel_id
        created_main_channel_id = main_channel_id
        # Resolve the channel object so rollback can call channel.delete
        # without round-tripping through the client cache (which may not
        # have observed the freshly-created channel yet).
        main_channel = await _resolve_category(adapter, main_channel_id)

        # Suppress Discord auto-thread for the orchestrator main channel
        # (P6).  Without this, the bot's first @mention reply would fork
        # the conversation into a new thread whose chat_id wouldn't match
        # the channel-scoped session_key, and the long-lived session
        # would never see operator messages again.  Best-effort: tests
        # use fake adapters without this attribute.
        no_thread_set = getattr(adapter, "_no_auto_thread_channels", None)
        if isinstance(no_thread_set, set):
            no_thread_set.add(main_channel_id)
        # Bypass DISCORD_REQUIRE_MENTION for the main channel so plain-text
        # operator messages reach the orchestrator without an @mention.
        free_set = getattr(adapter, "_orchestration_free_channels", None)
        if isinstance(free_set, set):
            free_set.add(main_channel_id)

        # Step 3: build the SessionSource for the new channel and
        # derive its session_key the same way the gateway's normal
        # path does — this guarantees the P5 fast-path will route
        # operator messages to our session.
        #
        # NOTE: per-channel session key (NOT per-user).  We deliberately
        # drop user_id/user_name so build_session_key sees no participant
        # id and emits a channel-scoped key — every operator in the
        # project channel must hit the same LongLivedSession, otherwise
        # collaborators would each spawn their own orphaned worker.
        new_source = SessionSource(
            platform=Platform.DISCORD,
            chat_id=main_channel_id,
            chat_type="group",
            user_id=None,
            user_name=None,
        )
        session_key = runner._session_key_for_source(new_source)

        # Step 4: construct the LongLivedSession + register.
        session = LongLivedSession(
            session_key,
            scope_id=scope_id,
            bot_user_id=getattr(adapter, "bot_user_id", None),
        )
        session.persona = ORCHESTRATOR
        session.slug = slug
        session.main_channel_id = main_channel_id
        runner._session_router.register(session_key, session)

        # Step 5: spawn the per-session worker.
        worker_task = asyncio.create_task(
            session_agent_worker(session, runner=runner, adapter=adapter),
            name=f"agent-worker:{session_key}",
        )
        session._in_flight = worker_task

        # Step 6: seed the inbox with the original requirement.
        # Re-use deliver_message so the bounded queue + admission
        # machinery applies uniformly (and any drop-on-full surface is
        # consistent with the live message path).
        #
        # NOTE: the synthetic event carries the *operator's* user_id (not
        # ``new_source``'s None).  The worker uses the first non-empty
        # user_id it sees as the approver for ``discord_wait_for_reaction``
        # auto-binding.  Without this, a requirement that goes straight
        # to plan approval would have no approver bound and the wait tool
        # would reject the call.
        seed_source = SessionSource(
            platform=Platform.DISCORD,
            chat_id=main_channel_id,
            chat_type="group",
            user_id=source.user_id,
            user_name=source.user_name,
        )
        accepted = await session.deliver_message(
            _synthesize_initial_event(seed_source, requirement)
        )
        if not accepted:
            # Should never happen — fresh session, empty inbox — but
            # guard anyway so a future change doesn't silently lose
            # the requirement.
            raise OrchestrationError(
                "session inbox refused the seeded requirement"
            )

        # Step 7 (P7a-1): write the initial project_runstate with
        # ``phase="draft"`` so P7b's rehydration / status tooling has
        # a starting point on disk.  Best-effort — runstate is
        # observability, never block bootstrap on an IO failure here.
        try:
            from hermes_cli.runstate import write_project_runstate

            write_project_runstate(
                scope_id or "",
                slug,
                phase="draft",
                main_channel_id=main_channel_id,
                merged_streams=[],
            )
        except Exception:
            logger.warning(
                "failed to write initial project_runstate for %s/%s",
                scope_id, slug, exc_info=True,
            )

        return f"✓ Project created — see <#{main_channel_id}>"

    except Exception as exc:
        logger.exception("project bootstrap failed for slug=%s", slug)
        # Rollback in reverse order: cancel worker → unregister
        # session → delete main channel → delete category.  Each step
        # is best-effort so a stuck cleanup can't shadow the original
        # failure.  We delete the channel BEFORE the category because
        # not all discord.py backends cascade child-channel deletes
        # when a non-empty category is deleted.
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()
            try:
                await worker_task
            except (asyncio.CancelledError, Exception):
                pass
        if session_key is not None:
            try:
                runner._session_router.unregister(session_key)
            except Exception:
                logger.exception("rollback: failed to unregister session")
        # Channel cleanup: prefer the object we already resolved; fall
        # back to id-based delete for the case where create succeeded
        # but the resolve right after it didn't (network race etc.).
        if main_channel is not None:
            await _best_effort_delete_channel(main_channel)
        elif created_main_channel_id:
            await _best_effort_resolve_and_delete_category_by_id(
                adapter, created_main_channel_id
            )
        # Drop the channel from the no-auto-thread set so a stale id
        # can't accumulate on the adapter forever after a failed bootstrap.
        if created_main_channel_id:
            no_thread_set = getattr(adapter, "_no_auto_thread_channels", None)
            if isinstance(no_thread_set, set):
                no_thread_set.discard(created_main_channel_id)
            free_set = getattr(adapter, "_orchestration_free_channels", None)
            if isinstance(free_set, set):
                free_set.discard(created_main_channel_id)
        # Category cleanup: same pattern.  Without the id-based fallback,
        # a failure between create_project_category and _resolve_category
        # would orphan the category forever.
        if category is not None:
            await _best_effort_delete_category(category)
        elif created_category_id:
            await _best_effort_resolve_and_delete_category_by_id(
                adapter, created_category_id
            )
        return f"⚠️ Bootstrap failed: {exc}"
