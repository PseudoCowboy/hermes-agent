"""Project status rollup (cross-stream-visibility phase).

The orchestrator main channel pins one rollup message per project that
gets edited in place as streams move from ``pending`` → ``working`` →
``complete``. This module owns:

- :func:`format_rollup_message` — render the current ``stream_status``
  map to a multi-line markdown body.
- :func:`update_stream_status` — merge a single stream's new status
  into the project runstate (RMW under the existing per-project lock),
  recompute the body, edit the pinned Discord message in place, and
  announce project completion once all streams pass review.

Both are best-effort: status is observability, not control flow. Any
failure (Discord API hiccup, missing rollup_message_id, bot offline)
is logged and swallowed so it never blocks the implementer agents.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)


# Map status → leading emoji used in the rollup. ``pending`` reads as
# "not started yet", ``working`` as "in progress now", ``complete`` as
# "implementer handed off". ``blocked`` is reserved for future use.
_STATUS_EMOJI: Dict[str, str] = {
    "pending": "⏳",
    "working": "🔨",
    "complete": "✅",
    "blocked": "⚠️",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_rollup_message(slug: str, stream_status: Mapping[str, Any]) -> str:
    """Compose the main-channel rollup message body.

    *stream_status* is the project-runstate ``stream_status`` map: keys
    are stream names, values are dicts with at least ``status`` and
    optionally ``role`` / ``channel_id`` / ``last_event``.

    The order is the iteration order of *stream_status*; bootstrap
    seeds it in plan order so this stays stable.
    """
    if not stream_status:
        return f"📦 `{slug}`\nStatus: provisioning streams…"
    lines = [f"📦 `{slug}`"]
    # Compute a top-line summary first.
    statuses = [
        (entry or {}).get("status", "pending") for entry in stream_status.values()
    ]
    if all(s == "complete" for s in statuses):
        lines.append("Status: ✅ all streams complete")
    elif any(s == "working" for s in statuses):
        lines.append("Status: 🔨 streams in progress")
    elif all(s == "pending" for s in statuses):
        lines.append("Status: ⏳ awaiting first turn")
    else:
        lines.append("Status: 🔄 mixed")
    for name, entry in stream_status.items():
        entry = entry or {}
        status = entry.get("status", "pending")
        emoji = _STATUS_EMOJI.get(status, "•")
        role = entry.get("role")
        chan = entry.get("channel_id")
        chan_part = f" → <#{chan}>" if chan else ""
        role_part = f" ({role})" if role else ""
        lines.append(f"• `{name}`{role_part} {emoji} {status}{chan_part}")
    return "\n".join(lines)


def format_completion_message(slug: str, stream_status: Mapping[str, Any]) -> str:
    """Compose the final main-channel completion announcement."""
    lines = [f"✅ `{slug}` complete", "All streams have passed review:"]
    for name, entry in stream_status.items():
        entry = entry or {}
        role = entry.get("role")
        role_part = f" ({role})" if role else ""
        lines.append(f"• `{name}`{role_part}")
    return "\n".join(lines)


def _all_streams_complete(stream_status: Mapping[str, Any]) -> bool:
    """Return True only when there is at least one stream and all are complete."""
    if not stream_status:
        return False
    return all(
        (entry or {}).get("status") == "complete"
        for entry in stream_status.values()
    )


def _find_orchestrator_session(runner: Any, scope_id: str, slug: str) -> Optional[Any]:
    """Walk the SessionRouter for the orchestrator's project session.

    The orchestrator session is identified by matching ``scope_id`` +
    ``slug`` and ``stream_name is None`` (streams have a stream_name).
    Returns the first match or ``None`` (gateway down, race condition
    during bootstrap, etc.).
    """
    router = getattr(runner, "_session_router", None)
    if router is None:
        return None
    sessions_dict = getattr(router, "_sessions", None)
    if not isinstance(sessions_dict, dict):
        return None
    # Snapshot to avoid mutation-during-iteration if a teardown races.
    for sess in list(sessions_dict.values()):
        if (
            getattr(sess, "scope_id", None) == scope_id
            and getattr(sess, "slug", None) == slug
            and getattr(sess, "stream_name", None) is None
        ):
            return sess
    return None


async def update_stream_status(
    *,
    runner: Any,
    scope_id: str,
    slug: str,
    stream_name: str,
    new_status: str,
    detail: Optional[str] = None,
    adapter: Any = None,
) -> bool:
    """Merge a stream-status transition into the runstate + edit the rollup.

    Returns True when the Discord rollup edit succeeds, False otherwise.
    All failures are logged + swallowed; the auto-emit hook calls this
    fire-and-forget and must never see an exception bubble up.

    *adapter* is required to actually edit the rollup. If omitted, the
    function still merges the runstate change so disk truth advances.
    """
    if new_status not in _STATUS_EMOJI:
        logger.debug(
            "update_stream_status: unknown status %r for %s/%s/%s",
            new_status, scope_id, slug, stream_name,
        )
        return False

    # Codex review (Important): the previous version did read-modify-write
    # *outside* any lock, then handed the merged map to
    # ``write_project_runstate`` which only acquired the lock for the
    # final read+write. Two concurrent stream updates could load the same
    # snapshot and clobber each other. Now the entire RMW is wrapped in
    # the same per-project lock the runstate writer uses, so a sibling
    # transition on the same project serialises behind us.
    from tools.workflow_tools import _project_lock
    from hermes_cli.runstate import (
        _atomic_write_text,
        _json_text,
        _project_skeleton,
        _read_existing,
        _read_project_runstate_snapshot,
        _runstate_path_project,
    )

    rollup_message_id = None
    main_channel_id = None
    body = None
    completion_body = None
    should_send_completion = False
    stream_status: Dict[str, Any] = {}

    with _project_lock(slug, scope_id):
        snapshot = _read_project_runstate_snapshot(scope_id, slug) or {}
        stream_status = dict(snapshot.get("stream_status") or {})
        entry = dict(stream_status.get(stream_name) or {})
        # Codex review (Important): make transitions monotonic so a stale
        # working-emit can't regress a stream that already reached
        # ``complete``. Order: pending(0) < working(1) < complete(2);
        # ``blocked`` is treated as a parallel state that cannot lower.
        order = {"pending": 0, "working": 1, "complete": 2, "blocked": 1}
        cur = entry.get("status") or "pending"
        if order.get(new_status, 0) < order.get(cur, 0):
            logger.debug(
                "update_stream_status: refusing regression %s -> %s for %s/%s/%s",
                cur, new_status, scope_id, slug, stream_name,
            )
            # Still report message_id success path? No — nothing to do.
            return False
        entry["status"] = new_status
        if detail is not None:
            entry["last_event"] = detail
        entry["updated_at"] = _now_iso()
        stream_status[stream_name] = entry

        rollup_message_id = snapshot.get("rollup_message_id")
        main_channel_id = snapshot.get("main_channel_id")

        # Prefer in-memory orchestrator-session attributes when present;
        # update them too so the next in-memory reader sees consistency.
        orch = (
            _find_orchestrator_session(runner, scope_id, slug)
            if runner else None
        )
        if orch is not None:
            if not main_channel_id:
                main_channel_id = (
                    getattr(orch, "main_channel_id", None) or main_channel_id
                )
            if not rollup_message_id:
                rollup_message_id = (
                    getattr(orch, "rollup_message_id", None) or rollup_message_id
                )
            try:
                orch.stream_status = stream_status
            except Exception:
                pass

        # Inline the project-runstate RMW under the SAME flock we already
        # hold. Calling ``write_project_runstate`` here would open a
        # second fd to the same lock file and try to acquire LOCK_EX
        # again — POSIX flock blocks that on a different fd from the
        # same process, deadlocking the gateway loop and stalling the
        # Discord heartbeat. The same fd-reentrancy footgun is already
        # documented at ``workflow_approve_plan`` (tools/workflow_tools.py:1318).
        try:
            path = _runstate_path_project(scope_id, slug)
            current = _read_existing(path) or _project_skeleton(scope_id, slug)
            current["scope_id"] = scope_id or ""
            current["slug"] = slug
            current["stream_status"] = stream_status
            if _all_streams_complete(stream_status):
                current["phase"] = "done"
                if adapter and main_channel_id and not (
                    current.get("completion_message_id")
                    or current.get("completion_announced_at")
                    or current.get("completion_announcement_inflight_at")
                ):
                    current["completion_announcement_inflight_at"] = _now_iso()
                    current.pop("completion_announcement_error", None)
                    should_send_completion = True
            current["updated_at"] = _now_iso()
            _atomic_write_text(path, _json_text(current))
        except Exception:
            logger.warning(
                "update_stream_status: inline runstate write failed for %s/%s",
                scope_id, slug, exc_info=True,
            )

        # Compute the body inside the lock so the rendered text matches
        # the freshly written disk snapshot.
        body = format_rollup_message(slug, stream_status)
        if should_send_completion:
            completion_body = format_completion_message(slug, stream_status)

    # 3) Edit the pinned rollup message OUTSIDE the lock so a slow
    # Discord call doesn't block sibling status writes.
    if not adapter or not main_channel_id:
        logger.debug(
            "update_stream_status: skip edit (adapter=%s rollup=%s chan=%s)",
            bool(adapter), bool(rollup_message_id), bool(main_channel_id),
        )
        return False

    edited = False
    if rollup_message_id:
        try:
            edit_result = await adapter.edit_message(
                main_channel_id, rollup_message_id, body,
            )
            # Codex review (Suggestion): respect SendResult.success rather
            # than treating "no exception" as success.
            ok = getattr(edit_result, "success", True)
            if ok is False:
                logger.warning(
                    "update_stream_status: edit_message returned success=False "
                    "for %s/%s msg=%s err=%s",
                    scope_id, slug, rollup_message_id,
                    getattr(edit_result, "error", None),
                )
            else:
                edited = True
        except Exception:
            logger.warning(
                "update_stream_status: edit_message failed for %s/%s msg=%s",
                scope_id, slug, rollup_message_id, exc_info=True,
            )
    else:
        logger.debug(
            "update_stream_status: no rollup_message_id for %s/%s; skipping edit",
            scope_id, slug,
        )

    if should_send_completion and completion_body:
        send_result = None
        try:
            send_for_role = getattr(adapter, "send_for_role", None)
            if callable(send_for_role):
                send_result = await send_for_role(
                    "orchestrator", main_channel_id, completion_body,
                )
            else:
                send_result = await adapter.send(main_channel_id, completion_body)
        except Exception as exc:
            logger.warning(
                "update_stream_status: completion announcement failed for %s/%s",
                scope_id, slug, exc_info=True,
            )
            send_result = exc

        completion_ok = getattr(send_result, "success", False) is True
        completion_message_id = getattr(send_result, "message_id", None)
        completion_error = getattr(send_result, "error", None) or str(send_result)

        try:
            with _project_lock(slug, scope_id):
                path = _runstate_path_project(scope_id, slug)
                current = _read_existing(path) or _project_skeleton(scope_id, slug)
                current["scope_id"] = scope_id or ""
                current["slug"] = slug
                current["phase"] = "done"
                current.pop("completion_announcement_inflight_at", None)
                if completion_ok:
                    current["completion_announced_at"] = _now_iso()
                    if completion_message_id is not None:
                        current["completion_message_id"] = str(completion_message_id)
                    current.pop("completion_announcement_error", None)
                else:
                    current["completion_announcement_error"] = completion_error
                current["updated_at"] = _now_iso()
                _atomic_write_text(path, _json_text(current))
        except Exception:
            logger.warning(
                "update_stream_status: completion runstate write failed for %s/%s",
                scope_id, slug, exc_info=True,
            )

    return edited
