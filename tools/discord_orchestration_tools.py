"""Tool wrappers around the Discord orchestration adapter.

Per ``plans/discord-orchestration-spec/02-technical-design.md`` §4, the
orchestrator and stream agents both need Discord operations exposed as
tools.  We split them across two toolsets:

- ``discord-orchestration-admin`` — project topology mutations
  (category create, channel create, project archive).  Orchestrator only.
- ``discord-orchestration-stream`` — per-channel ops (post message,
  react to message, wait for reaction).  Available to both orchestrator
  and stream agents.

All handlers reach the live :class:`gateway.platforms.discord.DiscordAdapter`
via the module-level singleton declared in
:mod:`gateway.platforms.discord_orchestration` (see ``set_active_adapter``).
That sidesteps threading the adapter through every ``ToolRegistry.dispatch``
call site while keeping the new module testable: tests inject fakes via
``set_active_adapter(fake_adapter)`` in a fixture.

Each handler returns a JSON string per the registry contract, and wraps
its own exceptions before returning so a missing-adapter or unknown-ID
case fails loud but predictably rather than NPE'ing.

Stream binding (auto-bind ``(scope_id, slug, stream_name)`` and reject
caller disagreement) lands in P5/P6 — the toolset bucketing here is
just the first half of §4's enforcement story.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from gateway.platforms.discord_orchestration import (
    OrchestrationError,
    archive_project_category,
    create_project_category,
    create_stream_channel,
    get_active_adapter,
)
from tools.registry import get_dispatch_context, registry

logger = logging.getLogger(__name__)


# =============================================================================
# Helpers
# =============================================================================


def _coerce_int_id(raw: Any, *, label: str) -> int:
    """Discord IDs are 64-bit ints but commonly ride as strings in JSON.

    Accept either; reject anything that doesn't round-trip cleanly so the
    failure is a clear OrchestrationError, not an opaque
    ``TypeError: '...' is not a valid integer``.
    """
    if isinstance(raw, bool):  # bool is an int subclass; we don't want it
        raise OrchestrationError(f"{label} must be an integer or string id, not bool")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    raise OrchestrationError(f"{label} must be a numeric id; got {raw!r}")


def _resolve_guild(guild_id: Any) -> Any:
    adapter = get_active_adapter()
    gid = _coerce_int_id(guild_id, label="guild_id")
    client = getattr(adapter, "_client", None)
    if client is None:
        raise OrchestrationError("active discord adapter has no client")
    guild = client.get_guild(gid)
    if guild is None:
        raise OrchestrationError(f"guild {gid} not found in client cache")
    return guild


def _resolve_channel(channel_id: Any) -> Any:
    """Resolve a channel ID to a discord.py channel object.

    Searches every guild the bot is in.  Discord channel IDs are globally
    unique so the first hit is authoritative.  Raises
    :class:`OrchestrationError` on miss.
    """
    adapter = get_active_adapter()
    cid = _coerce_int_id(channel_id, label="channel_id")
    client = getattr(adapter, "_client", None)
    if client is None:
        raise OrchestrationError("active discord adapter has no client")
    # discord.py exposes a top-level get_channel; fall back to scanning
    # guilds to keep the test fakes simple (the fakes implement
    # guild.get_channel and don't need a global lookup).
    getter = getattr(client, "get_channel", None)
    if callable(getter):
        ch = getter(cid)
        if ch is not None:
            return ch
    for guild in getattr(client, "guilds", []) or []:
        ch = guild.get_channel(cid) if hasattr(guild, "get_channel") else None
        if ch is not None:
            return ch
    raise OrchestrationError(f"channel {cid} not found in client cache")


def _ok(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _err(exc: Exception) -> str:
    """Return a JSON error body in the same shape as the registry's
    own error envelope so callers see a uniform contract regardless of
    whether the failure was caught here or by ``ToolRegistry.dispatch``.

    Non-:class:`OrchestrationError` exceptions (e.g. raw ``discord.py``
    ``Forbidden`` / ``NotFound`` / ``HTTPException``) are wrapped before
    being serialized, so every handler in this module presents the same
    error contract as the P3 admin helpers.
    """
    if not isinstance(exc, OrchestrationError):
        wrapped = OrchestrationError(
            f"{type(exc).__name__}: {exc}"
        )
        wrapped.__cause__ = exc
        exc = wrapped
    return json.dumps({"error": str(exc)}, ensure_ascii=False)


def _run_sync(coro: Any, *, timeout: float = 30.0) -> Any:
    """Run *coro* to completion from a sync handler, on the adapter's loop.

    The registry calls handlers synchronously from worker threads (see
    ``model_tools.run_async``).  Discord.py objects are bound to the
    gateway loop (``adapter._client.loop``) — driving them from a fresh
    ``asyncio.run()`` on this thread would deadlock or break loop
    affinity (``loop_id`` checks inside ``ClientWebSocketResponse``,
    locks created on the wrong loop, etc.).  So we marshal *coro* to the
    client's loop with :func:`asyncio.run_coroutine_threadsafe` and
    block on the result.

    *timeout* is the **outer** wait — how long this thread is willing to
    block before giving up on the future.  Callers whose coroutine has
    its own internal timeout (e.g. ``ReactionWaiter.wait(timeout=...)``)
    must pass an outer timeout strictly larger than the inner one, or
    the outer wait will fire first and rewrap a perfectly-good inner
    expiry as a generic "discord operation timed out" error.

    The disambiguation between *outer* and *inner* timeout matters
    because in Python 3.11+ ``asyncio.TimeoutError`` and
    ``concurrent.futures.TimeoutError`` are the same class.  We tell
    them apart by checking ``future.done()``: only an outer-wait expiry
    leaves the future un-finished.  Inner ``TimeoutError`` propagates
    untouched so handlers can map it to their own contract (e.g.
    ``discord_wait_for_reaction`` returning ``{"timed_out": True}``).

    For the special case where there is no live adapter loop yet
    (``ReactionWaiter`` tests use a fake adapter without a real client),
    fall back to a fresh ``asyncio.run()`` so the unit tests still work.
    """
    # Refuse if the calling thread is itself running a loop — we'd
    # deadlock waiting on a future from inside our own loop.
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        raise OrchestrationError(
            "discord orchestration tools cannot be dispatched from "
            "inside a running event loop without async support"
        )

    adapter = get_active_adapter()
    client = getattr(adapter, "_client", None)
    loop = getattr(client, "loop", None) if client is not None else None
    if loop is not None and loop.is_running():
        # Real Discord client: marshal onto its loop.
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            if not future.done():
                # Outer wait expired while the coroutine was still
                # running — that's the only case we want to wrap.
                future.cancel()
                raise OrchestrationError(
                    f"discord operation timed out after {timeout}s"
                ) from exc
            # Inner coroutine raised TimeoutError (e.g. asyncio.wait_for
            # inside ``ReactionWaiter.wait``).  Re-raise so handlers can
            # translate it into their own contract.
            raise

    # Fallback path used by unit tests with fake adapters whose fake
    # client has no live loop.  Safe because the fakes don't share state
    # across loops.
    return asyncio.run(coro)


# =============================================================================
# Schemas
# =============================================================================


_DISCORD_CREATE_PROJECT_CATEGORY_SCHEMA = {
    "name": "discord_create_project_category",
    "description": (
        "Create a new Discord category to host one project's stream "
        "channels.  Returns the category id (which is also the scope_id "
        "used by workflow storage and worktrees).  Orchestrator only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "guild_id": {
                "type": "string",
                "description": "Discord guild (server) id.",
            },
            "name": {
                "type": "string",
                "description": "Category name (1-100 chars).",
            },
            "reason": {
                "type": "string",
                "description": "Optional audit-log reason.",
            },
        },
        "required": ["guild_id", "name"],
    },
}


_DISCORD_CREATE_STREAM_CHANNEL_SCHEMA = {
    "name": "discord_create_stream_channel",
    "description": (
        "Create a text channel under a project category for one stream "
        "(frontend, backend, etc.).  Channel name must be a single "
        "lowercase segment safe for both Discord and a git worktree dir."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category_id": {
                "type": "string",
                "description": "Discord category id (== scope_id).",
            },
            "name": {
                "type": "string",
                "description": "Stream name; lowercase letters, digits, '-', '_'.",
            },
            "topic": {
                "type": "string",
                "description": "Optional channel topic.",
            },
            "reason": {
                "type": "string",
                "description": "Optional audit-log reason.",
            },
        },
        "required": ["category_id", "name"],
    },
}


_DISCORD_ARCHIVE_PROJECT_CATEGORY_SCHEMA = {
    "name": "discord_archive_project_category",
    "description": (
        "Archive a project: deletes channels then the category.  If "
        "keep_channel_ids is non-empty, those channels are preserved and "
        "the category is left in place so they remain reachable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category_id": {
                "type": "string",
                "description": "Discord category id to archive.",
            },
            "reason": {
                "type": "string",
                "description": "Optional audit-log reason.",
            },
            "keep_channel_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Channel ids to skip during archive.",
            },
        },
        "required": ["category_id"],
    },
}


_DISCORD_POST_MESSAGE_SCHEMA = {
    "name": "discord_post_message",
    "description": (
        "Post a message to a Discord channel.  Returns the message id so "
        "the caller can later attach reaction waits (clarification UX)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "Target channel id."},
            "content": {"type": "string", "description": "Message text."},
        },
        "required": ["channel_id", "content"],
    },
}


_DISCORD_REACT_TO_MESSAGE_SCHEMA = {
    "name": "discord_react_to_message",
    "description": (
        "Add an emoji reaction to a Discord message.  Used by the bot to "
        "seed approval affordances (✅ / ❌) on a clarification prompt."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string", "description": "Channel id of the message."},
            "message_id": {"type": "string", "description": "Target message id."},
            "emoji": {"type": "string", "description": "Emoji to add (single char or unicode)."},
        },
        "required": ["channel_id", "message_id", "emoji"],
    },
}


_DISCORD_WAIT_FOR_REACTION_SCHEMA = {
    "name": "discord_wait_for_reaction",
    "description": (
        "Block until the project owner reacts to a specific message with "
        "one of the allowed emojis, or the timeout fires.  Per design §3 "
        "the wait is keyed by (channel_id, message_id, user_id) so two "
        "concurrent clarifications in different channels can't cross-resolve."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "channel_id": {"type": "string"},
            "message_id": {"type": "string"},
            "user_id": {
                "type": "string",
                "description": "Only this user's reaction can satisfy the wait.",
            },
            "allowed_emojis": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Emojis that count.  Defaults to ✅ / ❌.",
            },
            "timeout_s": {
                "type": "number",
                "description": "Timeout in seconds.  Defaults to 600.",
            },
        },
        "required": ["channel_id", "message_id", "user_id"],
    },
}


# =============================================================================
# Handlers
# =============================================================================


def _handle_create_project_category(args: dict, **_kw: Any) -> str:
    try:
        guild = _resolve_guild(args.get("guild_id"))
        name = args.get("name") or ""
        reason = args.get("reason")
        out = _run_sync(
            create_project_category(guild=guild, name=name, reason=reason)
        )
        return _ok({
            "category_id": out.category_id,
            "name": out.name,
        })
    except Exception as exc:  # surfaced as JSON error envelope
        return _err(exc)


def _handle_create_stream_channel(args: dict, **_kw: Any) -> str:
    try:
        category = _resolve_channel(args.get("category_id"))
        name = args.get("name") or ""
        topic = args.get("topic")
        reason = args.get("reason")
        out = _run_sync(
            create_stream_channel(
                category=category, name=name, topic=topic, reason=reason
            )
        )
        return _ok({
            "channel_id": out.channel_id,
            "category_id": out.category_id,
            "name": out.name,
        })
    except Exception as exc:
        return _err(exc)


def _handle_archive_project_category(args: dict, **_kw: Any) -> str:
    try:
        category = _resolve_channel(args.get("category_id"))
        reason = args.get("reason")
        keep_raw = args.get("keep_channel_ids") or []
        if not isinstance(keep_raw, list):
            raise OrchestrationError("keep_channel_ids must be an array")
        keep_set = {
            str(_coerce_int_id(v, label="keep_channel_ids[]"))
            for v in keep_raw
        }

        def _filter(ch: Any) -> bool:
            return str(getattr(ch, "id", "")) not in keep_set

        result = _run_sync(
            archive_project_category(
                category=category,
                reason=reason,
                channel_filter=_filter if keep_set else None,
            )
        )
        return _ok(result)
    except Exception as exc:
        return _err(exc)


def _handle_post_message(args: dict, **_kw: Any) -> str:
    try:
        adapter = get_active_adapter()
        channel_id = _coerce_int_id(args.get("channel_id"), label="channel_id")
        content = args.get("content")
        if not isinstance(content, str) or not content:
            raise OrchestrationError("content must be a non-empty string")
        ctx = get_dispatch_context()
        role = getattr(ctx, "discord_bot_role", None) if ctx is not None else None
        send_for_role = getattr(adapter, "send_for_role", None)
        if role and callable(send_for_role):
            result = _run_sync(send_for_role(role, str(channel_id), content))
            if getattr(result, "success", False):
                return _ok({
                    "message_id": str(getattr(result, "message_id", "") or ""),
                    "channel_id": str(channel_id),
                    "role": role,
                })
            raise OrchestrationError(
                f"role bot send failed: {getattr(result, 'error', 'unknown error')}"
            )

        channel = _resolve_channel(channel_id)
        send = getattr(channel, "send", None)
        if not callable(send):
            raise OrchestrationError(
                f"channel {getattr(channel, 'id', '?')} has no send() method"
            )
        msg = _run_sync(send(content))
        return _ok({
            "message_id": str(getattr(msg, "id", "")),
            "channel_id": str(getattr(channel, "id", "")),
        })
    except Exception as exc:
        return _err(exc)


def _handle_react_to_message(args: dict, **_kw: Any) -> str:
    try:
        channel = _resolve_channel(args.get("channel_id"))
        message_id = _coerce_int_id(args.get("message_id"), label="message_id")
        emoji = args.get("emoji")
        if not isinstance(emoji, str) or not emoji:
            raise OrchestrationError("emoji must be a non-empty string")
        fetcher = getattr(channel, "fetch_message", None)
        if not callable(fetcher):
            raise OrchestrationError(
                f"channel {getattr(channel, 'id', '?')} has no fetch_message()"
            )

        async def _go() -> Any:
            msg = await fetcher(message_id)
            await msg.add_reaction(emoji)
            return msg

        msg = _run_sync(_go())
        return _ok({
            "message_id": str(getattr(msg, "id", message_id)),
            "emoji": emoji,
        })
    except Exception as exc:
        return _err(exc)


def _handle_wait_for_reaction(args: dict, **_kw: Any) -> str:
    try:
        adapter = get_active_adapter()
        waiter = getattr(adapter, "_reaction_waiter", None)
        if waiter is None:
            raise OrchestrationError(
                "active adapter has no _reaction_waiter (P3 wiring missing)"
            )
        channel_id = _coerce_int_id(args.get("channel_id"), label="channel_id")
        message_id = _coerce_int_id(args.get("message_id"), label="message_id")
        user_id = _coerce_int_id(args.get("user_id"), label="user_id")
        allowed = args.get("allowed_emojis")
        if allowed is not None and not isinstance(allowed, list):
            raise OrchestrationError("allowed_emojis must be an array of strings")
        timeout_raw = args.get("timeout_s", 600)
        try:
            timeout = float(timeout_raw)
        except (TypeError, ValueError) as exc:
            raise OrchestrationError(f"timeout_s must be a number; got {timeout_raw!r}") from exc

        async def _go() -> str:
            return await waiter.wait(
                channel_id, message_id, user_id,
                allowed_emojis=allowed, timeout=timeout,
            )

        try:
            # The inner ``waiter.wait(timeout=timeout)`` does the
            # semantic timeout; the outer ``_run_sync`` timeout is just
            # the worker-thread blocking ceiling, so it must be strictly
            # larger than ``timeout`` or the outer wait would fire first
            # and rewrap the inner expiry as a generic error.  Add a
            # 5s margin (and a 30s minimum so trivially-small inner
            # timeouts in tests still leave headroom).
            outer = max(timeout + 5.0, 30.0)
            emoji = _run_sync(_go(), timeout=outer)
        except asyncio.TimeoutError:
            return _ok({"timed_out": True})
        return _ok({"emoji": emoji, "timed_out": False})
    except Exception as exc:
        return _err(exc)


# =============================================================================
# Registry
# =============================================================================


def _discord_check() -> bool:
    """Tool is usable iff discord.py is importable.  We don't require an
    active adapter at registration time — the handler error message is
    clearer than silently filtering the schema out."""
    try:
        import discord  # noqa: F401
        return True
    except Exception:
        return False


registry.register(
    name="discord_create_project_category",
    toolset="discord-orchestration-admin",
    schema=_DISCORD_CREATE_PROJECT_CATEGORY_SCHEMA,
    handler=_handle_create_project_category,
    check_fn=_discord_check,
    emoji="📂",
)

registry.register(
    name="discord_create_stream_channel",
    toolset="discord-orchestration-admin",
    schema=_DISCORD_CREATE_STREAM_CHANNEL_SCHEMA,
    handler=_handle_create_stream_channel,
    check_fn=_discord_check,
    emoji="📺",
)

registry.register(
    name="discord_archive_project_category",
    toolset="discord-orchestration-admin",
    schema=_DISCORD_ARCHIVE_PROJECT_CATEGORY_SCHEMA,
    handler=_handle_archive_project_category,
    check_fn=_discord_check,
    emoji="🗄️",
)

registry.register(
    name="discord_post_message",
    toolset="discord-orchestration-stream",
    schema=_DISCORD_POST_MESSAGE_SCHEMA,
    handler=_handle_post_message,
    check_fn=_discord_check,
    emoji="💬",
)

registry.register(
    name="discord_react_to_message",
    toolset="discord-orchestration-stream",
    schema=_DISCORD_REACT_TO_MESSAGE_SCHEMA,
    handler=_handle_react_to_message,
    check_fn=_discord_check,
    emoji="👍",
)

registry.register(
    name="discord_wait_for_reaction",
    toolset="discord-orchestration-stream",
    schema=_DISCORD_WAIT_FOR_REACTION_SCHEMA,
    handler=_handle_wait_for_reaction,
    check_fn=_discord_check,
    emoji="⏳",
)
