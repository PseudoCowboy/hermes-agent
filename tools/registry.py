"""Central registry for all hermes-agent tools.

Each tool file calls ``registry.register()`` at module level to declare its
schema, handler, toolset membership, and availability check.  ``model_tools.py``
queries the registry instead of maintaining its own parallel data structures.

Import chain (circular-import safe):
    tools/registry.py  (no imports from model_tools or tool files)
           ^
    tools/*.py  (import from tools.registry at module level)
           ^
    model_tools.py  (imports tools.registry + all tool modules)
           ^
    run_agent.py, cli.py, batch_runner.py, etc.
"""

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolDispatchContext:
    """Per-call dispatch context for tools that need session-bound state (P6).

    The Discord orchestration phase introduces per-session agent workers
    (``gateway.session_agent_worker``) that already know the project's
    ``scope_id`` (Discord category id), ``slug`` (project slug), and
    eventually ``stream_name``.  Rather than force the agent to thread
    these through every tool call, the worker injects them here and
    individual tool handlers can fall back to context fields when the
    matching ``args`` key is absent.

    Important policy:

    * Auto-binding fills MISSING args only.  If the agent supplies an
      explicit ``scope_id`` in args, that value wins — context never
      silently overrides a caller-supplied value.
    * Per-call only.  ``ToolDispatchContext`` is passed into one
      ``registry.dispatch()`` call and discarded; nothing on the
      registry holds it across calls.

    Frozen so handlers can't accidentally mutate it.
    """

    scope_id: Optional[str] = None
    slug: Optional[str] = None
    stream_name: Optional[str] = None
    # Discord-specific bindings (P6).  ``channel_id`` is the orchestrator's
    # main channel; ``user_id`` is the human operator who can approve plans
    # via reaction.  Auto-bind only — caller-supplied values still win.
    channel_id: Optional[str] = None
    user_id: Optional[str] = None
    # Optional callable that returns True when the originating session has
    # been torn down (P6).  Late tool calls from a worker-thread agent run
    # whose asyncio task was already cancelled would otherwise mutate a
    # deleted project; checking this in dispatch lets the registry refuse
    # the call cleanly with a "session closed" error instead.
    closed_check: Optional[Callable[[], bool]] = field(default=None, compare=False)
    # Per-stream worktree root (P7a-2).  When set, the implementer worker
    # has bound its session to a git worktree on disk; tool handlers may
    # consult this for cwd discipline.  Auto-bound into args only via the
    # ``sandbox`` mechanism below — there is no ``worktree_root`` in
    # ``_AUTO_BIND_KEYS`` because no tool schema declares it.
    worktree_root: Optional[Path] = None
    # Per-stream :class:`tools.sandboxed_toolset.SandboxedToolset` (P7a-2).
    # When non-None and the tool name is in the sandbox's allowlist, the
    # registry forwards the call through the sandbox (which validates path
    # args and forces a workdir) before reaching the handler.  Typed
    # ``Any`` to avoid an import cycle with ``tools.sandboxed_toolset``.
    sandbox: Optional[Any] = field(default=None, compare=False)
    # Gateway asyncio loop + platform adapter (cross-stream-visibility phase).
    # Tools dispatched on a worker *thread* (via ``asyncio.to_thread``) need
    # both to fire-and-forget post Discord events back into the channel:
    #   ``asyncio.run_coroutine_threadsafe(adapter.send(...), loop)``.
    # Optional so tests / non-Discord call sites can omit them; the auto-emit
    # hook in :meth:`ToolRegistry.dispatch` silently skips when either is None.
    # ``compare=False`` keeps the dataclass hashable / cheap to compare even
    # though the loop & adapter are not value-types.
    loop: Optional[Any] = field(default=None, compare=False)
    adapter: Optional[Any] = field(default=None, compare=False)
    # Gateway runner reference (cross-stream-visibility phase).  Lets the
    # auto-emit hook resolve sibling sessions / project rollup state via
    # ``runner._session_router._sessions`` without importing the gateway.
    # Optional; the hook silently skips status-rollup updates when None.
    runner: Optional[Any] = field(default=None, compare=False)
    # Discord role identity for outbound agent-authored messages.  The
    # primary Discord adapter remains the inbound/admin owner; this value
    # selects an optional send-only role bot for visible agent speech.
    discord_bot_role: Optional[str] = None


# Tool-arg keys that auto-bind from context when the arg is missing or
# blank.  Tools register their interest by listing one of these keys in
# their schema; the dispatcher checks before calling the handler.
_AUTO_BIND_KEYS = ("scope_id", "slug", "stream_name", "channel_id", "user_id")


# Toolset allowlist for context auto-bind.  Even if a tool's schema
# happens to declare ``slug`` / ``user_id`` / etc., we only inject
# session context for tools that belong to one of these orchestration
# toolsets.  This prevents a future MCP tool with an unrelated ``slug``
# parameter from silently receiving the project slug.
#
# Add a toolset name here (or call ``register_auto_bind_toolset(...)``)
# when introducing a new orchestrator-side tool family that should
# benefit from auto-bind.
_AUTO_BIND_TOOLSETS: Set[str] = {
    "workflow-orchestrator-mutating",
    "workflow-orchestrator-readonly",
    # P7a-2 codex review (Important #6): the implementer worker exposes
    # ``workflow-readonly`` and ``workflow-stream-mutating`` so per-stream
    # workflow tools (state queries, progress logging) need the same
    # auto-bind treatment as orchestrator tools — otherwise the agent
    # has to supply ``project_name`` / ``stream`` itself, which is both
    # awkward and easy to get wrong. Auto-bind only fills *missing* keys
    # (a malicious explicit value still wins), so this is a UX fix, not
    # a security boundary; per-stream isolation remains the sandbox's
    # job for filesystem ops and the merge-queue gate for branch ops.
    "workflow-readonly",
    "workflow-stream-mutating",
    "discord-orchestration-admin",
    "discord-orchestration-stream",
}


def register_auto_bind_toolset(toolset: str) -> None:
    """Add *toolset* to the auto-bind allowlist.

    Tests that register fake tools under custom toolset names must call
    this (and ideally remove via ``unregister_auto_bind_toolset``) so
    the dispatch context fills their args.
    """
    _AUTO_BIND_TOOLSETS.add(toolset)


def unregister_auto_bind_toolset(toolset: str) -> None:
    """Remove *toolset* from the auto-bind allowlist."""
    _AUTO_BIND_TOOLSETS.discard(toolset)


# Process-wide ambient dispatch context (P6).  The session worker sets
# this before every ``agent.run_conversation`` turn so that every tool
# call made during the turn — regardless of how deeply nested it is in
# AIAgent / handle_function_call / registry.dispatch — picks up the
# session's scope_id / slug / stream_name without each layer having to
# thread an explicit ``context=`` kwarg through.
#
# Falls back to the explicit ``context=`` kwarg on dispatch() when set
# (tests / future callers).  Explicit kwarg wins over ambient, ambient
# wins over None.
_dispatch_context_var: "ContextVar[Optional[ToolDispatchContext]]" = ContextVar(
    "hermes_tool_dispatch_context", default=None
)


# Recursion guard for the sandbox interception (P7a-2).  When
# ``SandboxedToolset.dispatch`` validates args and then calls back into
# ``ToolRegistry.dispatch`` to actually execute the handler, we must NOT
# re-enter the sandbox or we'd loop forever.  The sandbox sets this flag
# for the duration of its inner dispatch call; ``dispatch`` checks it
# before doing the sandbox handoff.
_sandbox_pass_through: "ContextVar[bool]" = ContextVar(
    "hermes_tool_sandbox_pass_through", default=False
)


@contextmanager
def _sandbox_pass_through_scope():
    """Mark the current dispatch as a sandbox pass-through (skip re-interception)."""
    token = _sandbox_pass_through.set(True)
    try:
        yield
    finally:
        _sandbox_pass_through.reset(token)


# Reentrancy guard for the auto-emit progress hook (cross-stream-visibility
# phase).  When the hook posts a Discord event via ``adapter.send`` or
# triggers ``update_stream_status``, neither path should be intercepted by
# another auto-emit on a nested registry call.  Mirrors the
# ``_sandbox_pass_through`` pattern above.
_progress_emit_pass_through: "ContextVar[bool]" = ContextVar(
    "hermes_tool_progress_emit_pass_through", default=False
)


@contextmanager
def _progress_emit_scope():
    """Mark the current dispatch as inside the progress-emission helper."""
    token = _progress_emit_pass_through.set(True)
    try:
        yield
    finally:
        _progress_emit_pass_through.reset(token)


# One-shot tracker for the (scope_id, slug, stream_name) "working"
# transition (cross-stream-visibility phase).  We need to fire a
# ``update_stream_status(..., "working")`` exactly once per stream.  The
# set is process-local; disk-side ``stream_status`` in the project
# runstate is the recovery-truth source.  Lock guards add/contains.
import threading as _threading  # noqa: E402  (intentional late import)
_streams_seen_working: "Set[tuple[str, str, str]]" = set()
_streams_seen_working_lock = _threading.Lock()


def get_dispatch_context() -> "Optional[ToolDispatchContext]":
    """Return the ambient :class:`ToolDispatchContext`, if any.

    Public accessor over the private ``_dispatch_context_var`` ContextVar.
    Workflow handlers and the auto-emit hook need to read ``loop`` /
    ``adapter`` / ``stream_name`` without depending on the ContextVar
    being a stable name.
    """
    return _dispatch_context_var.get()


@contextmanager
def use_dispatch_context(context: "Optional[ToolDispatchContext]"):
    """Bind *context* as the ambient dispatch context for the duration.

    Used by ``gateway.session_agent_worker`` to scope a long-lived
    session's ``scope_id`` / ``slug`` to the tool calls made inside one
    agent turn.  Safe to nest: the outer context is restored on exit.
    """
    token = _dispatch_context_var.set(context)
    try:
        yield context
    finally:
        _dispatch_context_var.reset(token)


# ---------------------------------------------------------------------------
# Auto-emit progress events (cross-stream-visibility phase)
# ---------------------------------------------------------------------------
# Maps a qualifying mutating tool name to a formatter that takes
# (stream, args, result) and returns the one-line Discord message.
# Read-only tools (read_file, search_files, workflow_status, anything
# starting with discord_) MUST NOT appear here — that's the whole point.
_PROGRESS_PREVIEW_MAX = 120


def _preview(value: Any) -> str:
    """Compact, single-line preview of a value capped at 120 chars."""
    if value is None:
        return ""
    s = str(value).strip()
    s = " ".join(s.split())  # collapse internal whitespace
    if len(s) > _PROGRESS_PREVIEW_MAX:
        s = s[:_PROGRESS_PREVIEW_MAX - 1] + "…"
    return s


def _fmt_path(args: dict) -> str:
    """Pull a short basename from any path-bearing arg."""
    for key in ("path", "file_path"):
        v = args.get(key)
        if v:
            try:
                return Path(str(v)).name or str(v)
            except Exception:
                return str(v)
    return ""


def _fmt_terminal(args: dict) -> str:
    """Show only the executable name from a terminal command.

    Codex review (Critical): the previous version posted the full
    command, which routinely contains tokens / signed URLs / env
    assignments. We strip to the first whitespace-separated token and
    show a short suffix indicating arg count.
    """
    raw = args.get("command") or args.get("cmd") or ""
    s = str(raw).strip()
    if not s:
        return ""
    # First token only — works for ``foo --flag``, ``/usr/bin/foo a b``,
    # and single-arg invocations alike.
    parts = s.split(None, 1)
    head = parts[0]
    # Strip any path prefix so ``/usr/local/bin/python3`` -> ``python3``.
    try:
        head = Path(head).name or head
    except Exception:
        pass
    head = head[:32]
    extra_args = 0
    if len(parts) > 1:
        # Cheap arg-count for hint; not parsed shell-aware on purpose.
        extra_args = len(parts[1].split())
    suffix = f" (+{extra_args} args)" if extra_args else ""
    return f"{head}{suffix}"


def _safe_discord_text(text: str) -> str:
    """Neutralise Discord mentions in bot-generated messages.

    Codex review (Important): checkpoint notes, review summaries, and
    signal messages flow from agents into Discord channels. Without
    sanitization, an agent (or attacker-controlled tool output) could
    emit ``@everyone`` / ``@here`` / ``<@&role>`` / ``<#channel>``.
    Insert a zero-width space after the trigger char so Discord stops
    treating it as a mention. Idempotent and does not alter the visible
    intent of legitimate text.
    """
    if not text:
        return ""
    # \u200b is zero-width space; safe to append after `@`.
    return (
        text
        .replace("@everyone", "@\u200beveryone")
        .replace("@here", "@\u200bhere")
        .replace("<@", "<@\u200b")
        .replace("<#", "<#\u200b")
        .replace("<&", "<&\u200b")
    )


_PROGRESS_FORMATTERS: Dict[str, Callable[[str, dict, str], str]] = {
    "write_file": lambda stream, args, _r: f"✏️ `{stream}` wrote `{_fmt_path(args)}`",
    "patch": lambda stream, args, _r: f"✏️ `{stream}` patched `{_fmt_path(args)}`",
    "terminal": lambda stream, args, _r: (
        f"⚙️ `{stream}` ran `{_fmt_terminal(args)}`"
    ),
    "workflow_checkpoint": lambda stream, args, _r: (
        f"📋 `{stream}` checkpoint: {_safe_discord_text(_preview(args.get('note') or ''))}"
    ),
    "workflow_review_task": lambda stream, args, _r: (
        f"📋 `{stream}` review handoff: "
        f"{_safe_discord_text(_preview(args.get('summary') or ''))}"
    ),
    "workflow_stream_signal": lambda stream, args, _r: (
        f"📋 `{stream}` signaled `{args.get('target_stream', '')}`: "
        f"{_safe_discord_text(_preview(args.get('message') or ''))}"
    ),
}


def _result_is_error(result: Any) -> bool:
    """True if the tool result is a JSON object with a top-level ``error`` key."""
    if not isinstance(result, str):
        return False
    try:
        decoded = json.loads(result)
    except Exception:
        return False
    return isinstance(decoded, dict) and "error" in decoded


def _maybe_emit_tool_progress(
    name: str,
    args: dict,
    result: Any,
    ctx: "Optional[ToolDispatchContext]",
) -> None:
    """Best-effort: post a one-line stream-channel event after a successful tool call.

    Silent no-op when any precondition isn't met: ctx is None / not bound,
    no stream_name (orchestrator turn), no channel_id, no loop, no adapter,
    we're already inside the emission scope, the tool isn't in the
    qualifying mutating bucket, or the result parses to a top-level
    {"error": ...}. All exceptions are swallowed; observability MUST NOT
    fail a tool call.
    """
    if _progress_emit_pass_through.get():
        return
    if not isinstance(ctx, ToolDispatchContext):
        return
    stream = getattr(ctx, "stream_name", None)
    channel_id = getattr(ctx, "channel_id", None)
    loop = getattr(ctx, "loop", None)
    adapter = getattr(ctx, "adapter", None)
    if not stream or not channel_id or loop is None or adapter is None:
        return
    formatter = _PROGRESS_FORMATTERS.get(name)
    if formatter is None:
        return
    if _result_is_error(result):
        return
    try:
        line = formatter(stream, args or {}, result if isinstance(result, str) else "")
    except Exception:
        logger.debug("auto-emit formatter raised for tool=%s", name, exc_info=True)
        return
    # Schedule the Discord post + status transition under the emission
    # scope so a nested registry call (e.g. update_stream_status -> dispatch)
    # cannot recursively emit another progress event.
    try:
        with _progress_emit_scope():
            try:
                import asyncio as _asyncio
                role = getattr(ctx, "discord_bot_role", None)
                send_for_role = getattr(adapter, "send_for_role", None)
                if role and callable(send_for_role):
                    coro = send_for_role(role, channel_id, line)
                else:
                    coro = adapter.send(channel_id, line)
                _asyncio.run_coroutine_threadsafe(coro, loop)
            except Exception:
                logger.debug(
                    "auto-emit send failed for tool=%s ch=%s", name, channel_id,
                    exc_info=True,
                )
            # First-time-working transition for (scope_id, slug, stream_name).
            scope_id = getattr(ctx, "scope_id", None) or ""
            slug = getattr(ctx, "slug", None) or ""
            runner = getattr(ctx, "runner", None)
            if scope_id and slug and runner is not None:
                key = (scope_id, slug, stream)
                fire = False
                with _streams_seen_working_lock:
                    if key not in _streams_seen_working:
                        _streams_seen_working.add(key)
                        fire = True
                if fire:
                    try:
                        from gateway.project_status import update_stream_status
                        import asyncio as _asyncio2
                        _asyncio2.run_coroutine_threadsafe(
                            update_stream_status(
                                runner=runner,
                                scope_id=scope_id,
                                slug=slug,
                                stream_name=stream,
                                new_status="working",
                                adapter=adapter,
                            ),
                            loop,
                        )
                    except Exception:
                        logger.debug(
                            "working-transition schedule failed for %s/%s/%s",
                            scope_id, slug, stream, exc_info=True,
                        )

            # Complete-transition: a successful ``workflow_review_task``
            # with ``verdict="approved"`` is the implementer-handoff
            # signal per the cross-stream-visibility spec.
            #
            # Codex review (Important): require ``args["stream"]`` to
            # match the ambient ``ctx.stream_name``. Otherwise an
            # approval recorded against a sibling stream (or the wrong
            # task on this stream) would mark the WRONG rollup row as
            # complete. The handler-level ``_validate_review_actor``
            # already constrains who can record what; this guard is the
            # complementary piece on the rollup side.
            if (
                name == "workflow_review_task"
                and (args or {}).get("verdict") == "approved"
                and scope_id and slug and runner is not None
                and (args or {}).get("stream") == stream
            ):
                try:
                    from gateway.project_status import update_stream_status
                    import asyncio as _asyncio3
                    _asyncio3.run_coroutine_threadsafe(
                        update_stream_status(
                            runner=runner,
                            scope_id=scope_id,
                            slug=slug,
                            stream_name=stream,
                            new_status="complete",
                            detail="review approved",
                            adapter=adapter,
                        ),
                        loop,
                    )
                except Exception:
                    logger.debug(
                        "complete-transition schedule failed for %s/%s/%s",
                        scope_id, slug, stream, exc_info=True,
                    )
    except Exception:
        logger.debug("auto-emit outer failed for tool=%s", name, exc_info=True)


def _reset_streams_seen_working_for_tests() -> None:
    """Test-only helper: clear the one-shot working tracker."""
    with _streams_seen_working_lock:
        _streams_seen_working.clear()


class ToolEntry:
    """Metadata for a single registered tool."""

    __slots__ = (
        "name", "toolset", "schema", "handler", "check_fn",
        "requires_env", "is_async", "description", "emoji",
        "max_result_size_chars",
    )

    def __init__(self, name, toolset, schema, handler, check_fn,
                 requires_env, is_async, description, emoji,
                 max_result_size_chars=None):
        self.name = name
        self.toolset = toolset
        self.schema = schema
        self.handler = handler
        self.check_fn = check_fn
        self.requires_env = requires_env
        self.is_async = is_async
        self.description = description
        self.emoji = emoji
        self.max_result_size_chars = max_result_size_chars


class ToolRegistry:
    """Singleton registry that collects tool schemas + handlers from tool files."""

    def __init__(self):
        self._tools: Dict[str, ToolEntry] = {}
        self._toolset_checks: Dict[str, Callable] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Callable = None,
        requires_env: list = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        max_result_size_chars: int | float | None = None,
    ):
        """Register a tool.  Called at module-import time by each tool file."""
        existing = self._tools.get(name)
        if existing and existing.toolset != toolset:
            logger.warning(
                "Tool name collision: '%s' (toolset '%s') is being "
                "overwritten by toolset '%s'",
                name, existing.toolset, toolset,
            )
        self._tools[name] = ToolEntry(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            requires_env=requires_env or [],
            is_async=is_async,
            description=description or schema.get("description", ""),
            emoji=emoji,
            max_result_size_chars=max_result_size_chars,
        )
        if check_fn and toolset not in self._toolset_checks:
            self._toolset_checks[toolset] = check_fn

    def deregister(self, name: str) -> None:
        """Remove a tool from the registry.

        Also cleans up the toolset check if no other tools remain in the
        same toolset.  Used by MCP dynamic tool discovery to nuke-and-repave
        when a server sends ``notifications/tools/list_changed``.
        """
        entry = self._tools.pop(name, None)
        if entry is None:
            return
        # Drop the toolset check if this was the last tool in that toolset
        if entry.toolset in self._toolset_checks and not any(
            e.toolset == entry.toolset for e in self._tools.values()
        ):
            self._toolset_checks.pop(entry.toolset, None)
        logger.debug("Deregistered tool: %s", name)

    # ------------------------------------------------------------------
    # Schema retrieval
    # ------------------------------------------------------------------

    def get_definitions(self, tool_names: Set[str], quiet: bool = False) -> List[dict]:
        """Return OpenAI-format tool schemas for the requested tool names.

        Only tools whose ``check_fn()`` returns True (or have no check_fn)
        are included.
        """
        result = []
        check_results: Dict[Callable, bool] = {}
        for name in sorted(tool_names):
            entry = self._tools.get(name)
            if not entry:
                continue
            if entry.check_fn:
                if entry.check_fn not in check_results:
                    try:
                        check_results[entry.check_fn] = bool(entry.check_fn())
                    except Exception:
                        check_results[entry.check_fn] = False
                        if not quiet:
                            logger.debug("Tool %s check raised; skipping", name)
                if not check_results[entry.check_fn]:
                    if not quiet:
                        logger.debug("Tool %s unavailable (check failed)", name)
                    continue
            # Ensure schema always has a "name" field — use entry.name as fallback
            schema_with_name = {**entry.schema, "name": entry.name}
            result.append({"type": "function", "function": schema_with_name})
        return result

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def dispatch(self, name: str, args: dict, **kwargs) -> str:
        """Execute a tool handler by name.

        * Async handlers are bridged automatically via ``_run_async()``.
        * All exceptions are caught and returned as ``{"error": "..."}``
          for consistent error format.
        * If a ``context`` kwarg of type ``ToolDispatchContext`` is
          supplied (P6 session-bound dispatch), missing/blank values
          for ``scope_id`` / ``slug`` / ``stream_name`` in ``args``
          are filled in from the context.  Caller-supplied args always
          win — auto-binding only fills holes, it never overrides.
        * If the dispatch context carries a ``sandbox``
          (:class:`tools.sandboxed_toolset.SandboxedToolset`) and the
          tool is in its allowlist, the call is forwarded through the
          sandbox so paths get validated and ``workdir`` is forced into
          the per-stream worktree (P7a-2).
        """
        entry = self._tools.get(name)
        if not entry:
            return json.dumps({"error": f"Unknown tool: {name}"})
        # Auto-bind session context into args (P6).  Done in dispatch
        # rather than per-handler so every tool benefits without
        # touching the ~600 lines of registry.register(...) calls.
        # Pop "context" off kwargs so it never leaks to handlers that
        # don't accept it — only the dispatcher consumes it.  When no
        # explicit kwarg is supplied, fall back to the ambient
        # ``_dispatch_context_var`` set by the session worker.
        ctx = kwargs.pop("context", None)
        if ctx is None:
            ctx = _dispatch_context_var.get()
        if isinstance(ctx, ToolDispatchContext):
            # Closed-session short-circuit (P6).  When the session whose
            # worker thread is running this dispatch was torn down (e.g.
            # /reset, !new) while the agent was mid-turn, the asyncio
            # task got cancelled but ``agent.run_conversation`` is still
            # running on a thread and may issue more tool calls.  Refuse
            # them so they can't mutate the project we just deleted.
            closed_check = getattr(ctx, "closed_check", None)
            if callable(closed_check):
                # Fail closed: a broken safety gate must not silently
                # let mutating tool calls through.  If the check raises,
                # treat the session as closed and refuse the call.
                try:
                    is_closed = bool(closed_check())
                except Exception:
                    logger.warning(
                        "Tool %s closed_check raised; failing closed",
                        name, exc_info=True,
                    )
                    is_closed = True
                if is_closed:
                    return json.dumps(
                        {"error": "Session has been closed; tool call refused."}
                    )
            # Mutate a shallow copy so we don't surprise the caller's
            # args dict (some callers reuse it across retries).
            args = dict(args) if args else {}
            # Two-layer gate:
            #   (1) toolset allowlist — only orchestration toolsets opt
            #       in.  An MCP tool with an unrelated ``slug`` parameter
            #       cannot leak project metadata because its toolset
            #       isn't on the allowlist.
            #   (2) schema declares the key — even an orchestration tool
            #       only receives keys it advertises in its schema.
            if entry.toolset in _AUTO_BIND_TOOLSETS:
                declared = set()
                try:
                    params = entry.schema.get("parameters") or {}
                    props = params.get("properties") or {}
                    if isinstance(props, dict):
                        declared = set(props.keys())
                except Exception:
                    declared = set()
                for key in _AUTO_BIND_KEYS:
                    if key not in declared:
                        continue
                    supplied = args.get(key)
                    # Treat empty string / None as "missing" — the
                    # orchestrator persona prompt explicitly tells the
                    # agent to pass "" when it has no value, which lets
                    # context auto-bind fill it in.
                    if supplied in (None, ""):
                        ctx_val = getattr(ctx, key, None)
                        if ctx_val:
                            args[key] = ctx_val
                # ``project_name`` aliasing — workflow tool schemas accept
                # ``project_name`` (a free-text human name that gets
                # ``slugify()``'d), but the bootstrap-set identifier on
                # the session is ``slug``. Without aliasing, the agent
                # has to guess the slug or invent its own project_name,
                # which lands writes in a parallel project directory and
                # silently breaks the post-turn approval-marker hook
                # (which only looks under ``session.slug``).
                #
                # Strategy: when the schema declares ``project_name`` and
                # the agent left it blank/missing, fill it from
                # ``ctx.slug``. ``slugify(slug)`` is idempotent so the
                # downstream ``_handle_*(project_name=...)`` resolves to
                # the same on-disk path the bootstrap chose.
                if "project_name" in declared:
                    supplied_pn = args.get("project_name")
                    if supplied_pn in (None, ""):
                        ctx_slug = getattr(ctx, "slug", None)
                        if ctx_slug:
                            args["project_name"] = ctx_slug
            # Sandbox interception (P7a-2).  If the implementer worker
            # bound a SandboxedToolset on the context, route any tool the
            # sandbox claims through it.  The sandbox validates path args
            # + forces workdir, then calls back into ``dispatch`` to
            # execute the handler — the recursion guard below short-
            # circuits that re-entry so the sandbox runs exactly once.
            #
            # Tools NOT in the sandbox allowlist fall through to normal
            # dispatch.  ``enforce_allowlist=True`` (default) makes the
            # sandbox refuse unknown allowlisted tools at validate-time;
            # we don't second-guess that here.
            if not _sandbox_pass_through.get():
                sandbox = getattr(ctx, "sandbox", None)
                if sandbox is not None and getattr(sandbox, "is_allowed", None) is not None:
                    try:
                        if sandbox.is_allowed(name):
                            with _sandbox_pass_through_scope():
                                return sandbox.dispatch(name, args, **kwargs)
                    except Exception as exc:
                        # P7a-2 codex review (Important #3): fail CLOSED
                        # on a broken sandbox. The sandbox's job is to
                        # pin path/workdir args to the per-stream
                        # worktree; if it's malfunctioning we cannot
                        # know whether the call would have been rewritten,
                        # so dispatching the original handler with the
                        # raw (potentially escaping) args risks the very
                        # write-outside-worktree bug the sandbox is meant
                        # to prevent. Surface a structured error instead.
                        logger.exception(
                            "sandbox dispatch raised for %s; refusing to "
                            "execute handler unsandboxed: %s",
                            name, exc,
                        )
                        return json.dumps({
                            "error": (
                                f"sandbox error for tool {name!r}: "
                                f"{type(exc).__name__}: {exc}"
                            )
                        })
        try:
            if entry.is_async:
                from model_tools import _run_async
                result = _run_async(entry.handler(args, **kwargs))
            else:
                result = entry.handler(args, **kwargs)
        except Exception as e:
            logger.exception("Tool %s dispatch error: %s", name, e)
            return json.dumps({"error": f"Tool execution failed: {type(e).__name__}: {e}"})
        # Auto-emit progress event after a successful handler return.
        # Best-effort, fully internal: never raises past this boundary.
        # The helper's own preconditions (stream_name set, loop+adapter
        # present, qualifying tool, non-error result) decide whether to
        # actually post anything.
        try:
            _maybe_emit_tool_progress(name, args, result, ctx if isinstance(ctx, ToolDispatchContext) else None)
        except Exception:
            logger.debug("auto-emit hook raised (suppressed) for tool=%s", name, exc_info=True)
        return result

    # ------------------------------------------------------------------
    # Query helpers  (replace redundant dicts in model_tools.py)
    # ------------------------------------------------------------------

    def get_max_result_size(self, name: str, default: int | float | None = None) -> int | float:
        """Return per-tool max result size, or *default* (or global default)."""
        entry = self._tools.get(name)
        if entry and entry.max_result_size_chars is not None:
            return entry.max_result_size_chars
        if default is not None:
            return default
        from tools.budget_config import DEFAULT_RESULT_SIZE_CHARS
        return DEFAULT_RESULT_SIZE_CHARS

    def get_all_tool_names(self) -> List[str]:
        """Return sorted list of all registered tool names."""
        return sorted(self._tools.keys())

    def get_schema(self, name: str) -> Optional[dict]:
        """Return a tool's raw schema dict, bypassing check_fn filtering.

        Useful for token estimation and introspection where availability
        doesn't matter — only the schema content does.
        """
        entry = self._tools.get(name)
        return entry.schema if entry else None

    def get_toolset_for_tool(self, name: str) -> Optional[str]:
        """Return the toolset a tool belongs to, or None."""
        entry = self._tools.get(name)
        return entry.toolset if entry else None

    def get_emoji(self, name: str, default: str = "⚡") -> str:
        """Return the emoji for a tool, or *default* if unset."""
        entry = self._tools.get(name)
        return (entry.emoji if entry and entry.emoji else default)

    def get_tool_to_toolset_map(self) -> Dict[str, str]:
        """Return ``{tool_name: toolset_name}`` for every registered tool."""
        return {name: e.toolset for name, e in self._tools.items()}

    def is_toolset_available(self, toolset: str) -> bool:
        """Check if a toolset's requirements are met.

        Returns False (rather than crashing) when the check function raises
        an unexpected exception (e.g. network error, missing import, bad config).
        """
        check = self._toolset_checks.get(toolset)
        if not check:
            return True
        try:
            return bool(check())
        except Exception:
            logger.debug("Toolset %s check raised; marking unavailable", toolset)
            return False

    def check_toolset_requirements(self) -> Dict[str, bool]:
        """Return ``{toolset: available_bool}`` for every toolset."""
        toolsets = set(e.toolset for e in self._tools.values())
        return {ts: self.is_toolset_available(ts) for ts in sorted(toolsets)}

    def get_available_toolsets(self) -> Dict[str, dict]:
        """Return toolset metadata for UI display."""
        toolsets: Dict[str, dict] = {}
        for entry in self._tools.values():
            ts = entry.toolset
            if ts not in toolsets:
                toolsets[ts] = {
                    "available": self.is_toolset_available(ts),
                    "tools": [],
                    "description": "",
                    "requirements": [],
                }
            toolsets[ts]["tools"].append(entry.name)
            if entry.requires_env:
                for env in entry.requires_env:
                    if env not in toolsets[ts]["requirements"]:
                        toolsets[ts]["requirements"].append(env)
        return toolsets

    def get_toolset_requirements(self) -> Dict[str, dict]:
        """Build a TOOLSET_REQUIREMENTS-compatible dict for backward compat."""
        result: Dict[str, dict] = {}
        for entry in self._tools.values():
            ts = entry.toolset
            if ts not in result:
                result[ts] = {
                    "name": ts,
                    "env_vars": [],
                    "check_fn": self._toolset_checks.get(ts),
                    "setup_url": None,
                    "tools": [],
                }
            if entry.name not in result[ts]["tools"]:
                result[ts]["tools"].append(entry.name)
            for env in entry.requires_env:
                if env not in result[ts]["env_vars"]:
                    result[ts]["env_vars"].append(env)
        return result

    def check_tool_availability(self, quiet: bool = False):
        """Return (available_toolsets, unavailable_info) like the old function."""
        available = []
        unavailable = []
        seen = set()
        for entry in self._tools.values():
            ts = entry.toolset
            if ts in seen:
                continue
            seen.add(ts)
            if self.is_toolset_available(ts):
                available.append(ts)
            else:
                unavailable.append({
                    "name": ts,
                    "env_vars": entry.requires_env,
                    "tools": [e.name for e in self._tools.values() if e.toolset == ts],
                })
        return available, unavailable


# Module-level singleton
registry = ToolRegistry()


# ---------------------------------------------------------------------------
# Helpers for tool response serialization
# ---------------------------------------------------------------------------
# Every tool handler must return a JSON string.  These helpers eliminate the
# boilerplate ``json.dumps({"error": msg}, ensure_ascii=False)`` that appears
# hundreds of times across tool files.
#
# Usage:
#   from tools.registry import registry, tool_error, tool_result
#
#   return tool_error("something went wrong")
#   return tool_error("not found", code=404)
#   return tool_result(success=True, data=payload)
#   return tool_result(items)            # pass a dict directly


def tool_error(message, **extra) -> str:
    """Return a JSON error string for tool handlers.

    >>> tool_error("file not found")
    '{"error": "file not found"}'
    >>> tool_error("bad input", success=False)
    '{"error": "bad input", "success": false}'
    """
    result = {"error": str(message)}
    if extra:
        result.update(extra)
    return json.dumps(result, ensure_ascii=False)


def tool_result(data=None, **kwargs) -> str:
    """Return a JSON result string for tool handlers.

    Accepts a dict positional arg *or* keyword arguments (not both):

    >>> tool_result(success=True, count=42)
    '{"success": true, "count": 42}'
    >>> tool_result({"key": "value"})
    '{"key": "value"}'
    """
    if data is not None:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps(kwargs, ensure_ascii=False)
