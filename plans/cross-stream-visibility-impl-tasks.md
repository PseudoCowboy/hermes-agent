# Cross-stream visibility — implementation task breakdown

Source spec: `plans/cross-stream-visibility-phase-spec.md`
Phase: cross-stream visibility (auto-emit + workflow_stream_signal + main-channel rollup).

Tasks are bottom-up: data structures → emission helper → workflow_stream_signal → bootstrap rollup init → status transitions → prompt updates → wiring smoke. Each task is independently testable.

---

## A. Data + dispatch-context plumbing

### T1. Add gateway-side getter for the dispatch context
- Files: `tools/registry.py`
- Change: Expose a public `get_dispatch_context() -> Optional[ToolDispatchContext]` that returns `_dispatch_context_var.get()`. Workflow handlers and the future emission helper need to read `loop`, `adapter`, and `stream_name` without importing the private ContextVar.
- Test: `tests/tools/test_scope_id_context_injection.py` — add a case asserting `get_dispatch_context()` returns the bound ctx inside `bind_dispatch_context()` scope and `None` outside.

### T2. Wire loop+adapter into orchestrator and implementer dispatch contexts
- Files: `gateway/implementer_worker.py`, `gateway/session_agent_worker.py`
- Change: In each worker's `_build_dispatch_ctx`, capture `loop = asyncio.get_running_loop()` once at worker startup and populate the existing `ToolDispatchContext.loop` and `ToolDispatchContext.adapter` fields (already declared on the dataclass per spec §11–12). Adapter comes from the worker's existing reference.
- Test: `tests/gateway/test_implementer_worker.py` and `tests/gateway/test_session_agent_worker.py` — assert the built ctx has non-None `loop` and `adapter`.

### T3. Extend project runstate writer with rollup fields
- Files: `hermes_cli/runstate.py`
- Change: Allow `write_project_runstate(...)` to accept and persist `rollup_message_id: Optional[str]` and `stream_status: Optional[Mapping[str, dict]]` via the existing merge-friendly RMW path. Each stream entry shape is `{status, role, channel_id, last_event, updated_at}`. Add a private `_read_project_runstate_snapshot(scope_id, slug)` helper that returns the parsed dict (or `None`) without raising — distinct from the public `read_project_runstate()` stub which deliberately raises.
- Test: `tests/hermes_cli/test_runstate_writes.py` — add a case writing then re-writing with overlapping `stream_status` keys, asserting merge wins per-key and `rollup_message_id` survives across writes.

---

## B. Project status helper module

### T4. New `gateway/project_status.py` with rollup render + update helper
- Files: `gateway/project_status.py` [NEW]
- Change: Implement `_format_rollup_message(slug, stream_status)` returning a multi-line markdown body (one bullet per stream with emoji per status). Implement `async def update_stream_status(*, runner, scope_id, slug, stream_name, new_status, detail=None, adapter=None)`: RMW project runstate to merge the single stream's new status, recompute body, look up `(rollup_message_id, project_main_channel_id)` from the orchestrator session via `runner._session_router._sessions.values()` (filter `scope_id`+`slug`+`stream_name is None`), fallback to `_read_project_runstate_snapshot` from T3 if memory miss, then `await adapter.edit_message(main_channel_id, rollup_message_id, body)`. All failures logged + swallowed.
- Test: `tests/gateway/test_project_status_rollup.py` [NEW] — fake runner+adapter; assert renders 3-stream body in order, write+edit happens once, missing rollup_message_id is no-op + logs.

---

## C. Bootstrap rollup initialization

### T5. Stream-bootstrap captures rollup message id + writes initial status map
- Files: `gateway/stream_bootstrap.py`
- Change: At the existing post-approval send site (~line 529–533), replace the static summary with `_format_rollup_message(slug, initial_status)` from T4 (or a local renderer call), capture `result.message_id` from `adapter.send(...)`, then call `write_project_runstate(scope_id, slug, rollup_message_id=mid, stream_status={s.name: {"status":"pending","role":s.role,"channel_id":<stream_chan_id>,"last_event":None,"updated_at":<iso>} for s in streams})`. Also stash `rollup_message_id`, `stream_status`, and `project_main_channel_id` as free attributes on each stream `LongLivedSession` (find via the loop already iterating streams) and on the project orchestrator session (look up via `_session_router._sessions.values()` filter scope_id+slug+stream_name is None).
- Test: `tests/gateway/test_stream_bootstrap.py` — extend to assert `result.message_id` is captured, `write_project_runstate` is called with both new fields, orchestrator session has `rollup_message_id` attached, every stream session has `project_main_channel_id` attached.

### T6. Best-effort pin the rollup message
- Files: `gateway/platforms/discord.py`, `gateway/stream_bootstrap.py`
- Change: Add `[NEW] DiscordAdapter.pin_message(chat_id: str, message_id: str)` that wraps `channel.fetch_message(...).then(.pin())` with broad exception swallow + log. In stream_bootstrap, after capturing the rollup message_id, schedule `await adapter.pin_message(...)` best-effort (try/except + log). Tests with fake adapter only assert it gets called.
- Test: `tests/gateway/test_stream_bootstrap.py` — assert `pin_message` was invoked with the captured message id.

---

## D. Auto-emission hook

### T7. Add reentrancy guard ContextVar + scope helper for emission
- Files: `tools/registry.py`
- Change: Mirror the `_sandbox_pass_through` pattern: add `_progress_emit_pass_through: ContextVar[bool]` (default False) and a `_progress_emit_scope()` contextmanager that sets True / restores via token. Also add a module-level `_streams_seen_working: set[tuple[str,str,str]]` plus a `threading.Lock` for the one-shot working transition.
- Test: `tests/tools/test_registry.py` — assert nested entries do not re-trigger the scope, and `_streams_seen_working` is checked under the lock.

### T8. Implement `_maybe_emit_tool_progress` helper
- Files: `tools/registry.py`
- Change: New function `_maybe_emit_tool_progress(name, args, result, ctx)` that returns silently when any of these fail: `ctx is None`, `ctx.stream_name` empty, `ctx.channel_id` empty, `ctx.loop`/`ctx.adapter` None, `_progress_emit_pass_through.get()` is True, result parses to top-level `{"error": ...}`, or `name` not in the qualifying mutating bucket. The bucket maps name → formatter:
  - `write_file` → `f"✏️ \`{stream}\` wrote \`{path}\`"`
  - `patch` → `f"✏️ \`{stream}\` patched \`{path}\`"`
  - `terminal` → `f"⚙️ \`{stream}\` ran \`{cmd[:120]}\`"`
  - `workflow_checkpoint` → `f"📋 \`{stream}\` checkpoint: {note[:120]}"`
  - `workflow_review_task` → `f"📋 \`{stream}\` review handoff: {summary[:120]}"`
  - `workflow_stream_signal` → `f"📋 \`{stream}\` signaled \`{target}\`: {message[:120]}"`
  Inside `_progress_emit_scope()`: `asyncio.run_coroutine_threadsafe(ctx.adapter.send(ctx.channel_id, line), ctx.loop)`. All exceptions caught + `logger.debug`. Read-only tools (`read_file`, `search_files`, `workflow_status`, anything starting with `discord_`) MUST NOT be in the bucket.
- Test: `tests/tools/test_registry.py` — table-driven: each qualifying tool emits expected line; read-only tools and orchestrator-style ctx (no stream_name) emit nothing; error result suppresses; passthrough scope suppresses.

### T9. Hook the emission helper into `ToolRegistry.dispatch()`
- Files: `tools/registry.py`
- Change: At the normal handler return site (around line 459–466), capture the result into a local `result = ...`, then call `_maybe_emit_tool_progress(name, args, result, ctx)` before returning. Do NOT hook the outer sandbox handoff branch (line ~431) — sandboxed calls re-enter through the normal path. Do NOT hook the structured-error return at line 466 (handler raised).
- Test: `tests/tools/test_registry.py` — round-trip: register a fake `write_file` handler, bind a ctx with fake adapter recording sends, assert one event was scheduled. Plus `tests/tools/test_registry_sandbox_dispatch.py` — sandboxed `write_file` emits exactly once via the inner dispatch, never twice.

---

## E. Stream-status transitions

### T10. Wire `working` transition into the auto-emit helper
- Files: `tools/registry.py`
- Change: Inside `_maybe_emit_tool_progress`, after successfully scheduling the send, take the lock, check `(scope_id, slug, stream_name) in _streams_seen_working`; if not, add it and schedule `update_stream_status(..., new_status="working")` via `run_coroutine_threadsafe` on `ctx.loop`. Need access to `runner` — read from a new optional `ctx.runner` field if present, else skip the rollup update silently. (Hook handle for tests.)
- Files (also): `tools/registry.py` (add `runner: Optional[Any] = field(default=None, compare=False)` to `ToolDispatchContext`), `gateway/implementer_worker.py` (populate `runner=runner` in `_build_dispatch_ctx`), `gateway/session_agent_worker.py` (same).
- Test: `tests/tools/test_registry.py` — first qualifying call schedules `update_stream_status` with `working`; second call for same `(scope_id,slug,stream_name)` does not; different stream does.

### T11. Wire `complete` transition into implementer worker exit
- Files: `gateway/implementer_worker.py`
- Change: Wrap the worker's main loop body in `try/finally`. On clean exit (no exception, or normal `await session.recv()` return), call `await update_stream_status(runner=runner, scope_id=..., slug=..., stream_name=..., new_status="complete")`. On exception, mark `"complete"` anyway (or leave as `"working"` — keep simple, do `complete` on any exit to honor "stream done"). Best-effort wrapped.
- Test: `tests/gateway/test_implementer_worker.py` — fake adapter; drive worker through one turn then exit, assert `update_stream_status` was called with `complete`.

---

## F. workflow_stream_signal tool

### T12. Add WORKFLOW_STREAM_SIGNAL_SCHEMA and handler
- Files: `tools/workflow_tools.py`
- Change: Define `WORKFLOW_STREAM_SIGNAL_SCHEMA` (per spec §8 JSON shape). Add `def _handle_stream_signal(args: dict) -> str`: validate `target_stream != current stream_name`; read `project_runstate.json` via `_read_project_runstate_snapshot(scope_id, slug)` (T3); look up `stream_status[target_stream].channel_id`; fallback to reading `workstreams/<target>/runstate.json` `channel_id`; if either present and not equal to current stream's channel, schedule `asyncio.run_coroutine_threadsafe(ctx.adapter.send(target_channel_id, f"📨 from \`{source_stream}\`: {message}"), ctx.loop)` using `get_dispatch_context()` (T1). Return JSON `{"success": True, "source_stream":..., "target_stream":..., "channel_id":..., "message_id": <if available>}`. On lookup miss, return `{"error": "no sibling stream named ..."}`.
- Test: `tests/tools/test_workflow_tools.py` — sibling found → posts; sibling missing → error JSON; cross-project lookup blocked (different scope_id).

### T13. Register workflow_stream_signal in registry + toolset
- Files: `tools/workflow_tools.py`, `toolsets.py`
- Change: Register `workflow_stream_signal` with the existing workflow registration block; place into toolset `workflow-stream-mutating` so `IMPLEMENTER_TOOLSETS` includes it. Add to `_HERMES_WORKFLOW_STREAM_MUTATING_TOOLS` if that constant exists (verify during impl); orchestrator toolsets MUST NOT receive it.
- Test: `tests/tools/test_workflow_toolset_split.py` — assert `workflow_stream_signal` membership in implementer-allowed but not in orchestrator-allowed toolset.

### T14. Allow workflow_stream_signal through stream sandbox
- Files: `gateway/implementer_worker.py`
- Change: At the implementer sandbox build site (around line 156), pass `extra_tools=["workflow_stream_signal"]` to `build_stream_sandbox(...)`. Do NOT modify `tools/sandboxed_toolset.py` defaults — the extension point already exists.
- Test: `tests/tools/test_sandboxed_toolset_allowlist_consistent.py` — assert when called with `extra_tools=["workflow_stream_signal"]` the allowlist contains it; without, it does not.

---

## G. Prompts

### T15. Update implementer prompts to reflect auto-emit + signal
- Files: `prompts/implementer_backend.md`, `prompts/implementer_frontend.md`
- Change: Around lines 53–63 of each, replace "post a `discord_post_message` after each file edit" guidance with: "File edits, patches, and terminal commands now auto-narrate to your stream channel — do not duplicate them with `discord_post_message`. Reserve `workflow_checkpoint` for milestones." Add one paragraph teaching `workflow_stream_signal(target_stream, message)` for cross-stream coordination only when another stream needs the info.
- Test: smoke `grep -n "workflow_stream_signal" prompts/implementer_*.md` returns lines in both files.

### T16. Note system-managed status block in orchestrator prompt
- Files: `prompts/orchestrator.md`
- Change: Add one line under "Tool usage rules" stating the post-approval status block in the main channel is system-managed; do not edit or repost it.
- Test: `grep -n "system-managed" prompts/orchestrator.md` returns a line.

---

## H. Final wiring smoke

### T17. End-to-end import + smoke
- Files: none
- Change: Verify `from gateway.project_status import update_stream_status`, `from tools.registry import get_dispatch_context, ToolDispatchContext, _maybe_emit_tool_progress` import without error. Run targeted pytest from spec §Verification. Run full regression: `venv/bin/python -m pytest tests/gateway/ tests/tools/ tests/hermes_cli/ -q`.
- Test: shell.

---

## Order of execution

T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9 → T10 → T11 → T12 → T13 → T14 → T15 → T16 → T17.

Tasks T1-T3 unblock everything else. T4 unblocks C+E. T7-T9 unblock D+E. T12-T14 unblock F. T15-T16 are leaf tasks. T17 is final smoke.
