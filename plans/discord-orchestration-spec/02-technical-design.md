# Technical Design: Hermes Discord Orchestration v1

## Purpose

This document is the Hermes-specific implementation design that satisfies `01-product-spec.md`.

It intentionally contains repo-level decisions that do not belong in the product spec: file/module ownership, adapter behavior, toolset splits, model routing, worktree layout, and recovery mechanics.

## Chosen Architecture

- Single Discord bot process.
- One project category per project.
- One long-lived orchestrator control flow per project.
- One long-lived stream session per stream channel.
- Fresh test-agent spawns for per-stream and integration verification.
- Scoped workflow state under a per-project namespace.
- Per-stream git worktrees plus one per-project integration worktree.

## Primary Design Decisions

### 1. Session routing

- Discord remains a thin router.
- The gateway runner is the single authoritative dispatch point.
- Long-lived sessions do not reuse the default single-slot session flow.
- Long-lived sessions receive messages through `session.deliver_message(event)`.

Chosen mechanics:

- The adapter resolves `channel_id -> session`.
- The runner always performs transcript persistence and approval routing first.
- For long-lived sessions, the runner forwards to `deliver_message`.
- For default sessions, the current short-lived agent loop remains unchanged.

### 2. Clarification admission model

- Admission logic lives on the stream session, not in the adapter and not around the inbox consumer.
- A stream session has exactly one outstanding clarification wait at a time.
- `pending_prompt_message_id` has three states:
  - `None`
  - `SENTINEL_PENDING`
  - concrete Discord message ID

Qualification rule:

- author is the project owner, and
- a prompt is pending, and
- the message is either a native reply to the pending prompt or an `@bot` mention while the prompt is pending.

Load-bearing properties:

- one-shot admission under a lock,
- buffered handling during the sentinel window,
- no weaker fallback such as “any bot reply”.

### 3. Reaction waiting model

- Reaction waits are keyed by `(channel_id, message_id, user_id)`.
- Reactions during the sentinel window are buffered per session, not in a shared global user-keyed buffer.
- This avoids cross-session collisions when the same operator is simultaneously approving different prompts in different channels.

### 4. Tool boundary split

Discord toolsets are split into:

- `discord-orchestration-admin`
  - category creation
  - channel creation
  - project archive
- `discord-orchestration-stream`
  - stream progress posts
  - clarification requests
  - reaction waits

Workflow mutators are split into:

- `workflow-orchestrator-mutating`
  - project topology changes
  - plan lifecycle changes
- `workflow-stream-mutating`
  - stream progress mutation
  - stream state writes
  - stream review verdicts

Chosen enforcement:

- Stream sessions are auto-bound to `(scope_id, slug, stream_name)`.
- Caller-supplied disagreement is rejected.
- Stream agents cannot call project-topology mutators because those tools are not in their manifest.

### 5. Scoped workflow storage

- `scope_id` is the Discord category ID.
- Workflow state is stored under `groups/shared_project/<scope_id>/active/<slug>/`.
- Locking keys are tuples `(scope_id, slug)`.
- Temporary atomic-write files are also scoped under the same project root.

This prevents same-slug collisions across concurrent projects.

### 6. Worktree topology

- Per project integration worktree:
  - `.worktrees/<scope_id>/<slug>/_integration/`
- Per stream worktree:
  - `.worktrees/<scope_id>/<slug>/<stream>/`

Chosen branch layout:

- integration branch: `project/<scope_id>/<slug>/_integration`
- stream branch: `project/<scope_id>/<slug>/<stream>`

The integration branch uses `_integration` as a leaf segment (matching the
worktree directory convention) so that it is a peer of stream branches
under the same `project/<scope>/<slug>/` namespace.  Without this, git's
filesystem-backed ref storage would conflict: a ref
`refs/heads/.../slug` (file) prevents creating
`refs/heads/.../slug/<stream>` (needs `slug/` as a directory).

Why this design:

- no shared git index across concurrent streams,
- no sibling stream writes in the same working tree,
- clean place for final merged-state verification.

### 7. Merge policy

This design resolves reviewed content by exact approved revision.

- Per-stream approval records `approved_head_sha`.
- Merge eligibility is determined by `approved_head_sha`, not by the current stream branch tip.
- A later unreviewed commit on the stream branch does not automatically ship.

Chosen queue model:

- `merge_queue_scan()` is level-triggered.
- It scans all streams for approved-but-unmerged reviewed revisions.
- It runs on startup, on approval, on merge completion, and after relevant resumes.
- Actual merge execution is serialized by a per-project `asyncio.Lock`.

### 8. Testing flow

Two-stage verification:

1. Per-stream verification in the stream worktree.
2. Final integration verification in `_integration/` after all required merges.

Chosen behavior:

- Test agents are fresh spawns, not long-lived sessions.
- Test agents classify criteria as automatable or human-judgement.
- Human-judgement criteria are posted for reaction-based operator approval.
- Automated failure records `changes_requested` and does not auto-loop indefinitely.

### 9. Agent roles and model routing

Pinned v1 routing:

- Orchestrator: Claude `claude-opus-4-7`, max effort.
- Plan reviewer: Codex `gpt-5.4`, xhigh.
- Frontend implementer: Gemini `gemini-3.1-pro`.
- Backend implementer: Claude `claude-opus-4-7`, max effort.
- Test agent: Codex `gpt-5.4`, xhigh.
- Web search: Gemini `gemini-3.1-flash`.

Routing policy:

- `agentRole` is required for code streams.
- Allowed values are `frontend` and `backend`.
- Ambiguous streams default to `backend`.
- Implementers do not web search in v1.

### 10. Safety boundary

`SandboxedToolset` is mistake resistance, not a true security sandbox.

Enforced:

- path-allowlisted `read` / `write` / `edit`,
- forced `cwd` for terminal,
- rejection of straightforward absolute-path escapes,
- stream-bound workflow mutators.

Not guaranteed:

- containment against a motivated adversarial agent,
- process-level isolation,
- kernel or container isolation.

### 11. Startup hygiene

Chosen startup order:

1. Rehydrate stream and project runstate.
2. Rebuild the expected worktree set from manifests.
3. Compare expected worktrees to on-disk worktrees.
4. Log mismatches without destructive cleanup.
5. Run `git worktree prune` only.
6. Require explicit operator `!cleanup` for destructive removal.

### 12. Pause and recovery model

Stream runstate is stored in `workstreams/<stream>/runstate.json`.

Key fields:

- `status`
- `prompt_message_id`
- `awaiting_user_id`
- `channel_id`
- `deadline_ts`
- `turn_count`
- `approved_head_sha`
- `reason`

Project runstate is stored in `project_runstate.json`.

Key fields:

- `phase`
- `active_merge_stream`
- `merged_streams`
- `integration_test_started_ts`
- `reason`

Chosen recovery policies:

- `running` on restart demotes to a paused state.
- Sentinel-window crashes demote to paused states instead of guessing outcome.
- Mid-merge crashes inspect git state before deciding whether to resume, pause, or requeue.
- Integration-test crashes do not auto-rerun.

## Repo Surface

Primary file ownership from the chosen design:

- `gateway/platforms/discord.py`
- `gateway/platforms/base.py`
- `tools/discord_orchestration_tools.py`
- `tools/sandboxed_toolset.py`
- `tools/gemini_search_tool.py`
- `tools/workflow_tools.py`
- `toolsets.py`
- `hermes_cli/agent_pool.py`
- `hermes_cli/runstate.py`
- `hermes_cli/orchestrator.py`
- `prompts/orchestrator.md`
- `prompts/plan_reviewer.md`
- `prompts/implementer_frontend.md`
- `prompts/implementer_backend.md`
- `prompts/test_agent.md`
- `tests/workflow/test_orchestration_e2e.py`

## Implementation Phases

1. Scope-id plumbing.
2. Worktree creation and sandbox wrapper.
3. Discord admin adapter methods and reaction waiter.
4. Orchestration tools and toolset split.
5. Session-level routing via `deliver_message`.
6. Agent pool and runstate.
7. Orchestrator, merge coordinator, and test-agent personas.
8. Real Discord bring-up.

## Open Implementation Follow-Ups

- Exact `discord.py` overwrite object shape.
- Whether search results are cached per requirement.
- Whether `!continue` resets counters from scratch or resumes accumulated counters.
- Whether `!new` accepts attachments in v1.

## Design Notes

- This design is intentionally more specific than the product spec.
- If the codebase later implements the same product behavior with a different module layout, this document should be updated without changing the product spec.
