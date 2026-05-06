# Cross-stream visibility phase spec

## Goal

Implement cross-stream visibility for Discord orchestration by making stream work visible without relying on implementer prompt discipline: qualifying tool calls auto-post concise progress lines to the stream channel, streams can directly signal another stream with `workflow_stream_signal(target_stream, message)`, and the project main channel contains one pinned rollup message that is edited in place as every stream moves from `pending` to `working` to `complete`. This phase directly supports stream execution visibility and isolation requirements in the product spec (`plans/discord-orchestration-spec/01-product-spec.md:92`, `plans/discord-orchestration-spec/01-product-spec.md:98`, `plans/discord-orchestration-spec/01-product-spec.md:150`) and the acceptance checks for D2, E2, E6, and the Codex/Claude/Chrome development loop (`plans/discord-orchestration-spec/03-acceptance-scenario.md:67`, `plans/discord-orchestration-spec/03-acceptance-scenario.md:76`, `plans/discord-orchestration-spec/03-acceptance-scenario.md:80`, `plans/discord-orchestration-spec/03-acceptance-scenario.md:134`).

## Non-goals

Do not implement a test-agent, merge-agent, merge queue, final integration flow, or a new honest `I'm blocked` tool in this phase. Do not redesign stream routing through an orchestrator hub: `workflow_stream_signal` is direct point-to-point. Do not change the stream workspace sandbox model beyond the specific allowlist addition described below, and do not broaden read-only tool visibility: read-only tools continue to emit no progress lines.

## Current anchors

- `ToolDispatchContext` already has `loop` and `adapter` fields appended for this phase; build on those fields rather than re-adding them (`tools/registry.py:78`, `tools/registry.py:86`, `tools/registry.py:87`).
- Dispatch already resolves ambient `ToolDispatchContext` and auto-binds selected args from `_AUTO_BIND_KEYS` and `_AUTO_BIND_TOOLSETS` (`tools/registry.py:93`, `tools/registry.py:105`, `tools/registry.py:342`, `tools/registry.py:379`, `tools/registry.py:388`).
- The existing sandbox recursion guard is `_sandbox_pass_through` plus `_sandbox_pass_through_scope()` (`tools/registry.py:154`, `tools/registry.py:160`, `tools/registry.py:166`, `tools/registry.py:431`). Use this as the pattern for the progress-emission reentrancy guard.
- The handler return site for the post-handler hook is the normal execution block in `ToolRegistry.dispatch()` (`tools/registry.py:459`, `tools/registry.py:462`, `tools/registry.py:463`, `tools/registry.py:466`).
- `build_stream_sandbox()` defaults to `read_file`, `write_file`, `patch`, `search_files`, and `terminal`, with `extra_tools` as the designed extension point (`tools/sandboxed_toolset.py:204`, `tools/sandboxed_toolset.py:212`, `tools/sandboxed_toolset.py:226`, `tools/sandboxed_toolset.py:233`).
- Implementer sessions currently build the sandbox at startup and bind dispatch context per turn (`gateway/implementer_worker.py:276`, `gateway/implementer_worker.py:279`, `gateway/implementer_worker.py:285`, `gateway/implementer_worker.py:288`, `gateway/implementer_worker.py:345`, `gateway/implementer_worker.py:352`).
- Orchestrator sessions bind their own dispatch context per turn (`gateway/session_agent_worker.py:437`, `gateway/session_agent_worker.py:440`, `gateway/session_agent_worker.py:489`, `gateway/session_agent_worker.py:496`, `gateway/session_agent_worker.py:503`).
- Stream bootstrap creates stream sessions, writes initial stream runstate, writes project runstate, and posts the current summary message (`gateway/stream_bootstrap.py:384`, `gateway/stream_bootstrap.py:395`, `gateway/stream_bootstrap.py:400`, `gateway/stream_bootstrap.py:515`, `gateway/stream_bootstrap.py:531`, `gateway/stream_bootstrap.py:533`, `gateway/stream_bootstrap.py:548`).
- `LongLivedSession` has no `__slots__`, already stores live per-session attributes, and `SessionRouter` keeps live sessions in `_sessions` (`gateway/session_router.py:90`, `gateway/session_router.py:121`, `gateway/session_router.py:146`, `gateway/session_router.py:156`, `gateway/session_router.py:341`, `gateway/session_router.py:343`, `gateway/session_router.py:356`). Use that existing free-attribute stash pattern for the rollup state.
- Discord message editing already exists as `DiscordAdapter.edit_message()` (`gateway/platforms/discord.py:922`, `gateway/platforms/discord.py:935`, `gateway/platforms/discord.py:939`). There is no `pin_message` helper in the current tree; adding one must be marked `[NEW]`.
- Project runstate writes are merge-friendly read-modify-write operations and accept arbitrary extra fields except where explicitly validated (`hermes_cli/runstate.py:198`, `hermes_cli/runstate.py:259`, `hermes_cli/runstate.py:286`, `hermes_cli/runstate.py:291`, `hermes_cli/runstate.py:293`).
- Workflow tool schemas and registry registrations are colocated in `tools/workflow_tools.py` (`tools/workflow_tools.py:1510`, `tools/workflow_tools.py:1694`, `tools/workflow_tools.py:1760`, `tools/workflow_tools.py:2706`, `tools/workflow_tools.py:2798`, `tools/workflow_tools.py:2831`). `rg -n "inject_runner"` currently returns no matches, so any runner injection helper must be `[NEW]`; this phase should avoid requiring runner injection for the signal path.
- Implementer prompts still tell agents to manually post progress, which this phase should revise because progress is now partly automatic (`prompts/implementer_backend.md:53`, `prompts/implementer_backend.md:62`, `prompts/implementer_frontend.md:54`, `prompts/implementer_frontend.md:63`).

## Data shape

Project runstate gains two fields using the existing RMW writer:

```json
{
  "rollup_message_id": "123456789012345678",
  "stream_status": {
    "frontend": {
      "status": "pending",
      "role": "frontend",
      "channel_id": "234567890123456789",
      "last_event": null,
      "updated_at": "2026-04-29T00:00:00Z"
    }
  }
}
```

`status` is one of `pending`, `working`, or `complete`. `pending` is written during stream bootstrap. `working` is written exactly once on the first successful qualifying stream tool call. `complete` is written when the stream successfully performs its implementation-completion handoff, currently defined as successful `workflow_review_task` with `verdict="approved"`; this is only "stream implementation complete", not test-agent verification or merge approval.

In memory, the project/orchestrator `LongLivedSession` stores `stream_status` and `rollup_message_id` as free attributes. Each stream `LongLivedSession` stores `project_main_channel_id`, `rollup_message_id`, and a reference to the same `stream_status` mapping where possible. Bootstrap writes the same initial map to disk, then attaches these attributes while it still has both the stream sessions and the router available (`gateway/stream_bootstrap.py:384`, `gateway/stream_bootstrap.py:395`, `gateway/session_router.py:341`). If in-memory state is missing after a restart, the dispatcher-side rollup update may rebuild from disk and still edit via `rollup_message_id`; rehydrating full workers remains out of scope.

Add `[NEW]` fields to `ToolDispatchContext` only if needed by the hook: `project_main_channel_id`, `rollup_message_id`, and `stream_status` with `compare=False`. The already-present `loop` and `adapter` fields are mandatory for actual Discord sends/edits from worker threads (`tools/registry.py:78`, `tools/registry.py:86`).

## Approach

1. **Bind gateway loop and adapter into dispatch context.** In `gateway/implementer_worker.py`, capture `loop = asyncio.get_running_loop()` in `implementer_worker()` before `_build_dispatch_ctx()` and populate the existing `ToolDispatchContext.loop` and `ToolDispatchContext.adapter` fields in `_build_dispatch_ctx()` (`gateway/implementer_worker.py:285`, `gateway/implementer_worker.py:288`). Do the same in `gateway/session_agent_worker.py` for orchestrator turns (`gateway/session_agent_worker.py:437`, `gateway/session_agent_worker.py:440`). The auto-emitter should normally require `ctx.stream_name` so orchestrator planning calls do not clutter the main channel, but the orchestrator context still gets loop/adapter for future gateway-owned workflow tools.

2. **Seed the rollup message during stream bootstrap.** Replace the current one-shot summary post with the initial rollup body at the existing post-approval send site (`gateway/stream_bootstrap.py:529`, `gateway/stream_bootstrap.py:533`). Capture `SendResult.message_id` from `adapter.send()` and write `rollup_message_id` plus the initial `stream_status` map through `write_project_runstate()` (`gateway/stream_bootstrap.py:515`, `hermes_cli/runstate.py:259`). Best-effort pin the message after send with `[NEW] DiscordAdapter.pin_message(chat_id, message_id)` or a local `[NEW] _pin_message_best_effort()` helper; `edit_message` exists but pinning does not (`gateway/platforms/discord.py:922`). Keep `_format_summary_message()` as the rollup renderer or replace it with `[NEW] _format_rollup_message(stream_status)` at the same helper site (`gateway/stream_bootstrap.py:548`).

3. **Attach rollup state to live sessions.** In `gateway/stream_bootstrap.py`, after creating each stream `LongLivedSession` and before registering/starting the worker, set `session.project_main_channel_id = main_channel_id`, `session.stream_status = stream_status`, and later `session.rollup_message_id = rollup_message_id` (`gateway/stream_bootstrap.py:384`, `gateway/stream_bootstrap.py:391`, `gateway/stream_bootstrap.py:395`, `gateway/stream_bootstrap.py:417`). Also find the project/orchestrator session by scanning `runner._session_router._sessions.values()` for matching `scope_id`, `slug`, and `stream_name is None`, then attach the same `stream_status` and `rollup_message_id` there (`gateway/session_router.py:341`). This uses the existing live-session stash pattern and does not add persistent session-store schema.

4. **Add a registry post-handler progress hook.** In `ToolRegistry.dispatch()`, change the normal handler return block from direct `return` to `result = ...`, then call `[NEW] _maybe_emit_tool_progress(name, args, result, entry, ctx)` before returning `result` (`tools/registry.py:459`, `tools/registry.py:462`, `tools/registry.py:463`). Do not run the hook in the outer sandbox handoff branch; sandboxed calls re-enter the registry and hit the normal handler return path once (`tools/registry.py:431`, `tools/registry.py:436`, `tools/sandboxed_toolset.py:197`). If the handler raises and dispatch returns the structured error at `tools/registry.py:466`, do not emit.

5. **Implement the exact auto-emission contract.** The hook fires only when all are true: `ctx` is a `ToolDispatchContext`, `ctx.stream_name` is non-empty, `ctx.channel_id` is non-empty, `ctx.loop` and `ctx.adapter` are present, the result does not parse as a top-level JSON `{"error": ...}`, and the tool is in a qualifying mutating bucket. Read-only tools emit nothing: `read_file`, `search_files`, and `workflow_status` are explicitly silent. Qualifying events and line formats are:
   - `write_file`: `✏️ `<stream>` wrote `<path>``
   - `patch`: `✏️ `<stream>` patched `<path>``
   - `terminal`: `⚙️ `<stream>` ran `<command-preview>``
   - `workflow_checkpoint`: `📋 `<stream>` checkpoint: <note-preview>`
   - `workflow_review_task`: `📋 `<stream>` review handoff: <summary-preview>`
   - `workflow_stream_signal`: `📋 `<stream>` signaled `<target_stream>`: <message-preview>`
   The preview limit should be small and deterministic, e.g. 120 characters after whitespace normalization. Use direct `asyncio.run_coroutine_threadsafe(ctx.adapter.send(ctx.channel_id, line), ctx.loop)` from the worker thread and swallow/log failures; observability must not fail the tool call.

6. **Guard reentrancy and the one-time working transition.** Add `[NEW] _progress_emit_pass_through: ContextVar[bool]` and `[NEW] _progress_emit_scope()` using the same token/reset structure as `_sandbox_pass_through_scope()` (`tools/registry.py:160`, `tools/registry.py:166`). Wrap the scheduled send/edit work so an accidental tool call or nested dispatch cannot recursively emit another progress event. Add `[NEW] _streams_seen_working: set[tuple[str, str, str]]` plus a `threading.Lock`; when the first qualifying event for `(scope_id, slug, stream_name)` passes the hook, update that stream to `working`, write the project `stream_status`, and edit the rollup. The set is a process-local duplicate suppressor; disk runstate remains the source of recovery truth.

7. **Update the rollup on status changes.** Add `[NEW] gateway/stream_status.py` or local helpers in `gateway/stream_bootstrap.py` for `_format_rollup_message(stream_status)` and `update_stream_status(...)`. The helper updates the in-memory mapping if supplied, writes the full `stream_status` map through `write_project_runstate()` (`hermes_cli/runstate.py:286`, `hermes_cli/runstate.py:291`), then calls `adapter.edit_message(project_main_channel_id, rollup_message_id, rendered)` (`gateway/platforms/discord.py:922`). If `rollup_message_id` or `project_main_channel_id` is unavailable in memory, read the project runstate directly with `[NEW] read_project_runstate_snapshot(scope_id, slug)` or a private helper in `hermes_cli/runstate.py`; do not use the existing public `read_project_runstate()` stub because it deliberately raises (`hermes_cli/runstate.py:301`).

8. **Add `workflow_stream_signal`.** In `tools/workflow_tools.py`, add `WORKFLOW_STREAM_SIGNAL_SCHEMA` beside the other workflow schemas (`tools/workflow_tools.py:1694`, `tools/workflow_tools.py:1760`) and register it in the registry section (`tools/workflow_tools.py:2706`). The schema is:

   ```json
   {
     "name": "workflow_stream_signal",
     "description": "Send a concise coordination message from the current stream to another stream in the same project.",
     "parameters": {
       "type": "object",
       "properties": {
         "project_name": {"type": "string"},
         "scope_id": {"type": "string"},
         "stream_name": {"type": "string", "description": "Source stream; auto-filled from context."},
         "target_stream": {"type": "string"},
         "message": {"type": "string"}
       },
       "required": ["project_name", "target_stream", "message"]
     }
   }
   ```

   Existing auto-bind behavior fills `scope_id` and `stream_name` because those keys are in `_AUTO_BIND_KEYS`, and fills `project_name` from `ctx.slug` because the dispatcher aliases it when the schema declares `project_name` (`tools/registry.py:93`, `tools/registry.py:400`, `tools/registry.py:414`). Do not auto-bind `target_stream`; the caller must choose it. Register under `toolset="workflow-stream-mutating"` so stream agents receive it through `IMPLEMENTER_TOOLSETS` (`gateway/implementer_worker.py:65`, `gateway/implementer_worker.py:68`). Add it to `_HERMES_WORKFLOW_STREAM_MUTATING_TOOLS` (`toolsets.py:88`) and keep `model_tools.py` unchanged because `tools.workflow_tools` is already discovered (`model_tools.py:161`).

9. **Resolve and send the direct signal.** The handler should validate source and target stream names under the same project lock/pattern used by `workflow_checkpoint()` (`tools/workflow_tools.py:2240`, `tools/workflow_tools.py:2275`, `tools/workflow_tools.py:2284`). It should find the target channel id from `project_runstate.json` `stream_status[target_stream].channel_id`, falling back to `workstreams/<target>/runstate.json` `channel_id` written during bootstrap (`gateway/stream_bootstrap.py:400`, `gateway/stream_bootstrap.py:404`). To access the current adapter/loop, add `[NEW] get_dispatch_context()` in `tools/registry.py` rather than importing `_dispatch_context_var` directly. Send the target-channel line as `📨 from `<source_stream>`: <message>` via `asyncio.run_coroutine_threadsafe(ctx.adapter.send(target_channel_id, line), ctx.loop)` and return JSON containing `success`, `source_stream`, `target_stream`, `channel_id`, and `message_id` if available. This is direct point-to-point and never asks the orchestrator to relay.

10. **Allow the signal through the stream sandbox.** Keep `build_stream_sandbox()` defaults narrow (`tools/sandboxed_toolset.py:226`). At the implementer sandbox build site, pass `extra_tools=["workflow_stream_signal"]` so the signal is explicitly allowed through the same per-stream dispatch gate without broadening global defaults (`gateway/implementer_worker.py:150`, `gateway/implementer_worker.py:156`, `tools/sandboxed_toolset.py:233`). Update the allowlist consistency tests to include this extra-tool path rather than expecting the default set to grow.

11. **Revise prompts.** In `prompts/implementer_backend.md` and `prompts/implementer_frontend.md`, change the manual progress guidance so implementers still call `workflow_checkpoint` at milestones but do not spam `discord_post_message` just to report file edits or command execution; those are automatic now (`prompts/implementer_backend.md:53`, `prompts/implementer_backend.md:62`, `prompts/implementer_frontend.md:54`, `prompts/implementer_frontend.md:63`). Add a short instruction to use `workflow_stream_signal` for cross-stream coordination only when another stream needs the information.

## Test plan

- `tests/tools/test_registry.py`: unit coverage for `_maybe_emit_tool_progress`, result-error suppression, read-only silence, event formatting, and reentrancy guard.
- `tests/tools/test_registry_sandbox_dispatch.py`: verify sandboxed `write_file`/`patch` emit once from the inner dispatch path and do not duplicate from the outer sandbox branch.
- `tests/tools/test_scope_id_context_injection.py`: cover any new `ToolDispatchContext` fields and `[NEW] get_dispatch_context()` without breaking existing auto-bind semantics.
- `tests/tools/test_workflow_tools.py`: cover `workflow_stream_signal` validation, target lookup, missing target channel, successful send, and JSON return shape.
- `tests/tools/test_workflow_toolset_split.py`: assert `workflow_stream_signal` is in `workflow-stream-mutating` and not in orchestrator-only mutating tools.
- `tests/tools/test_sandboxed_toolset_allowlist_consistent.py`: assert `extra_tools=["workflow_stream_signal"]` is accepted and registered while defaults remain unchanged.
- `tests/hermes_cli/test_runstate_writes.py`: assert project runstate preserves `stream_status` and `rollup_message_id` across merge-friendly writes.
- `tests/gateway/test_stream_bootstrap.py`: cover initial rollup render, send-result message id capture, best-effort pin call, session free-attribute attachment, and disk runstate fields.
- `tests/gateway/test_implementer_worker.py`: assert implementer dispatch context carries `loop`, `adapter`, rollup fields, and `stream_status` reference.
- `tests/gateway/test_session_agent_worker.py`: assert orchestrator dispatch context also carries `loop` and `adapter` without stream auto-emission.
- `tests/gateway/test_discord_send.py` or a new `tests/gateway/test_discord_rollup_edit.py`: cover `DiscordAdapter.edit_message()` integration assumptions and `[NEW] pin_message` if added.

## Verification

Run commands with the required environment activation first:

```bash
source venv/bin/activate
python -m pytest tests/tools/test_registry.py tests/tools/test_registry_sandbox_dispatch.py tests/tools/test_workflow_tools.py -q
python -m pytest tests/gateway/test_stream_bootstrap.py tests/gateway/test_implementer_worker.py tests/hermes_cli/test_runstate_writes.py -q
python -m pytest tests/tools/test_workflow_toolset_split.py tests/tools/test_sandboxed_toolset_allowlist_consistent.py -q
```

Smoke the gateway path by running the Discord gateway in a test profile, approving a two-stream project, and confirming: the main channel gets exactly one pinned rollup message; stream channels receive auto progress lines for `write_file`, `patch`, `terminal`, and `workflow_checkpoint`; `workflow_status`, `read_file`, and `search_files` do not emit; `workflow_stream_signal` posts directly into the target stream channel; and the main-channel rollup edits in place from `pending` to `working` to `complete`.

Manual Chrome DevTools verification must drive the Discord web app through the §M acceptance loop: create/approve a project, observe the main channel rollup, inspect both stream channels, trigger a direct stream signal, and capture snapshots of the main channel plus each stream channel as required by `plans/discord-orchestration-spec/03-acceptance-scenario.md:142`.

## Risks and mitigations

- **Duplicate progress lines from sandbox re-entry.** Mitigate by placing the hook only after the real handler result and adding `_progress_emit_pass_through`, following the existing `_sandbox_pass_through` pattern (`tools/registry.py:154`, `tools/registry.py:431`).
- **Tool calls fail because Discord send/edit fails.** Mitigate by making all progress sends and rollup edits best-effort with logging only; return the original tool result unchanged.
- **Rollup state diverges between memory and disk.** Mitigate by writing the full `stream_status` map on every transition through `write_project_runstate()` and treating in-memory maps as caches (`hermes_cli/runstate.py:259`, `hermes_cli/runstate.py:291`).
- **Stale `_streams_seen_working` after restart suppresses nothing.** Acceptable: the set is process-local optimization only; disk `stream_status` prevents rendering regressions after helper reads runstate.
- **`workflow_stream_signal` leaks across projects.** Mitigate by resolving target streams only within `project_path(slug, scope_id)` and using auto-bound `scope_id`/`project_name`; never search global channel lists (`tools/registry.py:400`, `tools/workflow_tools.py:2275`).
- **Rollup message deleted or unpinned by a human.** Mitigate by logging edit failures and optionally posting a replacement rollup only from an explicit recovery path; do not silently create multiple rollups during normal event emission.
- **Prompt conflicts with auto progress.** Mitigate by updating implementer prompts so agents keep meaningful checkpoints but stop duplicating file/command activity with `discord_post_message`.

## Out of scope

Test-agent implementation, merge-agent implementation, final merge/integration state transitions, and an honest `I'm blocked` tool are explicitly deferred. This phase may show a stream as `complete` only for implementation handoff visibility; it does not claim verification acceptance or merge eligibility.
