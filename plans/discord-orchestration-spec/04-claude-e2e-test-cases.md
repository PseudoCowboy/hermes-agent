# Claude E2E Test Cases: Discord Orchestration Verification

## Purpose

This document is written for an operator who wants Claude to execute and verify the Discord orchestration system end to end.

Claude should treat `01-product-spec.md` as the behavioral contract and `03-acceptance-scenario.md` as the pass/fail gate.

## Execution Rules For Claude

- Do not change the implementation while running verification unless the operator explicitly switches from testing to fixing.
- Treat every failing expected result as a real defect unless the operator confirms the spec should change.
- Record concrete evidence for every case: Discord messages, channel names, file paths, runstate snippets, and relevant git output.
- If a case blocks later cases, mark it `blocked` and explain why.
- If a case is long-running because of a 15-minute default timeout, prefer a test environment override only if the operator explicitly provides one. Otherwise wait and record the real behavior.

## Test Environment Preconditions

- A Discord test guild exists.
- The Hermes bot is installed in that guild.
- The bot has the permissions required to create categories, create channels, and apply visibility overwrites.
- The orchestration implementation under test is deployed from the intended branch.
- Claude has access to:
  - the Discord transcript,
  - the repo workspace,
  - bot logs,
  - git worktrees created by the system.
- The active workspace is clean enough that new worktree and runstate artifacts can be attributed to this test run.

## Evidence Format

For each test case, Claude should record:

- `Status`: pass, fail, blocked, or not run.
- `Evidence`:
  - Discord message or reaction evidence.
  - Filesystem evidence.
  - Git evidence.
  - Runstate evidence.
- `Notes`: short explanation of why the case passed or failed.

## Common Test Inputs

Project A:

```text
!new build a Chrome extension that shows a Chinese translation when I double-click an English word. Dictionary should be user-editable, offline, no network calls.
```

Project B:

```text
!new build a hello-world flask app on port 5000
```

## Test Cases

### TC-01 Project creation

Goal:
Validate initial project bring-up and Discord topology.

Steps:

1. Send Project A in the home channel.
2. Observe category creation.
3. Observe `#{slug}-main` creation.
4. Confirm stream channels are not created yet.
5. Confirm an orchestrator introduction or next-step post appears in `#{slug}-main`.

Expected results:

- Owner-only category exists.
- `#{slug}-main` exists immediately.
- No stream channels exist before plan approval.
- The system is waiting for clarification or proceeding to plan.

Evidence to capture:

- Category name.
- Channel list under the category.
- First orchestrator post in `#{slug}-main`.

### TC-02 Clarification gating

Goal:
Verify that only qualified replies advance a pending clarification.

Steps:

1. Wait for a clarification prompt in `#{slug}-main`.
2. Send a plain non-reply owner message.
3. Confirm the system does not advance.
4. Send a native reply to the pending clarification prompt.
5. If possible, race a second qualifying reply immediately after the first.

Expected results:

- Plain message is transcribed but ignored for admission.
- Native reply unblocks the clarification.
- Second qualifying reply to the same wait is dropped.

Evidence to capture:

- Prompt message ID.
- Plain message transcript.
- Reply message transcript.
- Orchestrator behavior after each message.

### TC-03 Plan posting and review loop

Goal:
Verify that the plan is reviewed before implementation and is posted with explicit control actions.

Steps:

1. Let the system draft and post the plan.
2. Inspect the plan content.
3. Confirm it contains explicit approval, change, and rejection instructions.
4. Confirm implementation has not started yet.

Expected results:

- Plan appears in `#{slug}-main`.
- Project A plan has at least three streams.
- Plan includes the required Word Translator coverage.
- No stream channel or work execution begins before approval.

Evidence to capture:

- Posted plan text.
- Any reviewer notes block.
- Absence of stream execution before approval.

### TC-04 Approval, decomposition, and model routing

Goal:
Verify stream provisioning after approval.

Steps:

1. Send `approve` in `#{slug}-main`.
2. Observe stream channel creation and review channel creation.
3. Inspect the generated worktree layout.
4. Inspect logs or runtime metadata to confirm stream role to model routing.

Expected results:

- Stream channels are created only after approval.
- One workspace exists per stream and one integration workspace exists.
- Frontend streams use the configured frontend model.
- Backend streams use the configured backend model.

Evidence to capture:

- Channel list.
- Worktree paths.
- Runtime logs showing stream role/model mapping.

### TC-05 Workspace isolation

Goal:
Verify that streams stay within their own workspace roots.

Steps:

1. Let at least two streams begin implementation.
2. Inspect git status per stream worktree.
3. Attempt or observe a simple cross-workspace read or edit through normal tool paths.

Expected results:

- Each stream modifies only files in its own workspace.
- Sibling workspace access through normal tool paths is rejected.

Evidence to capture:

- `git status` or equivalent per worktree.
- Rejection evidence for blocked cross-workspace access.

### TC-06 Stream clarification and timeout

Goal:
Verify stream-local blocking and resume behavior.

Steps:

1. Wait for a stream clarification request in a stream channel.
2. Reply correctly and confirm the stream resumes.
3. Trigger or wait for another stream clarification request.
4. Leave it unanswered until timeout.
5. Send `!continue <stream>` from the main channel after timeout.

Expected results:

- Answered stream clarification resumes promptly.
- Timed-out clarification pauses only that stream.
- Resume targets the paused stream without disturbing other streams.

Evidence to capture:

- Clarification prompt text.
- Timeout evidence.
- Runstate before and after `!continue <stream>`.

### TC-07 Per-task progress reporting

Goal:
Verify that implemented tasks produce operator-readable progress updates.

Steps:

1. Observe a stream completing at least one task.
2. Inspect the stream channel for the progress post.

Expected results:

- After `implemented` task transitions, a concise update appears.
- The post includes task identity, touched files, and next task.

Evidence to capture:

- Progress post text.
- Matching task state transition if available.

### TC-08 Per-stream verification and reviewed-revision pinning

Goal:
Verify stream review flow and ensure only the reviewed revision becomes merge-eligible.

Steps:

1. Let one stream reach implementation complete.
2. Observe the test agent verifying the stream in the stream workspace.
3. Confirm automatable and human-judgement criteria are handled separately.
4. Approve the stream if a human reaction is required.
5. Before the stream merges, create or observe an additional unreviewed commit on the same stream branch if the environment allows this safely.
6. Inspect whether merge eligibility remains pinned to the reviewed revision.

Expected results:

- Verification evidence is posted.
- Approval records a reviewed revision.
- Later unreviewed changes do not auto-ship.

Evidence to capture:

- Review channel posts.
- Recorded approved revision.
- Branch tip versus reviewed revision evidence.

### TC-09 Merge serialization and conflict handling

Goal:
Verify project-level merge serialization and merge-conflict recovery.

Steps:

1. Let two streams become merge-eligible close together.
2. Observe merge execution order.
3. Seed or trigger a merge conflict.
4. Confirm the project pauses.
5. Resolve the conflict manually in the integration workspace or abort it explicitly.
6. Send `!continue <slug>` or the documented abort path.

Expected results:

- Merges do not race.
- Merge conflict pauses the project, not just one stream.
- Resume logic verifies the merge outcome instead of guessing.

Evidence to capture:

- Merge logs.
- `project_runstate.json` transitions.
- Git evidence from `_integration/`.

### TC-10 Final integration verification and done state

Goal:
Verify final project-level validation.

Steps:

1. Allow all required streams to merge.
2. Observe final integration verification in the integration workspace.
3. Confirm the project reaches done only after success.

Expected results:

- Integration verification runs after all required merges.
- Success transitions the project to done.
- Main channel gets a completion summary.

Evidence to capture:

- Review channel integration-verification post.
- Final project state.
- Main-channel completion summary.

### TC-11 Archive

Goal:
Verify archive behavior.

Steps:

1. Send `!archive` for a completed project.
2. Inspect archive storage.
3. Inspect worktree cleanup.
4. Inspect Discord category cleanup.

Expected results:

- Active project state moves to archive storage.
- Active worktrees are removed.
- Project Discord topology is deleted.

Evidence to capture:

- Archived path.
- Removed worktree evidence.
- Deleted category evidence.

### TC-12 Parallel project isolation

Goal:
Verify that a second project can run simultaneously without leakage.

Steps:

1. While Project A still has active stream work, send Project B in the home channel.
2. Let both projects reach at least clarification or stream execution.
3. Inspect Discord, filesystem, and workflow-state separation.

Expected results:

- Separate categories and channels.
- Separate scoped workflow roots.
- Separate worktree roots.
- No clarification cross-talk.

Evidence to capture:

- Both project category trees.
- Both workflow-state roots.
- Both worktree roots.

### TC-13 Crash recovery during clarification

Goal:
Verify conservative recovery for a stream awaiting clarification.

Steps:

1. Wait until a stream is blocked on clarification.
2. Kill the bot process.
3. Optionally send a qualifying owner reply while the bot is down if the environment allows.
4. Restart the bot.
5. Inspect recovery behavior.

Expected results:

- Recovery either rehydrates the wait and backfills the reply or pauses conservatively if delivery was indeterminate.
- The system does not silently lose the pending clarification state.

Evidence to capture:

- Runstate before crash.
- Reply timing evidence.
- Runstate and channel behavior after restart.

### TC-14 Crash recovery during merge and integration verification

Goal:
Verify conservative project-level recovery.

Steps:

1. Kill the bot during an integration merge attempt.
2. Restart and inspect recovery.
3. Kill the bot during final integration verification.
4. Restart and inspect recovery.

Expected results:

- Mid-merge restart inspects git state and reconciles or pauses safely.
- Mid-integration-verification restart pauses conservatively and does not auto-rerun silently.

Evidence to capture:

- `project_runstate.json` before and after restart.
- Git merge state.
- Restart logs.

### TC-15 Safety caps and change-request gating

Goal:
Verify guardrails that should reject or pause work.

Steps:

1. Force or simulate turn-cap exceedance.
2. Force or simulate wall-clock exceedance.
3. Attempt a sixth concurrent project while five are active.
4. Send `!change <description>` while streams are still active.
5. Send `!change <description>` while the project is paused.
6. Send `!change <description>` when the project is idle.

Expected results:

- Turn-cap and wall-clock exceedance pause only the affected stream.
- Sixth project is rejected.
- `!change` is rejected while active.
- `!change` is rejected while paused.
- `!change` is accepted only when idle and starts a new reviewed plan revision.

Evidence to capture:

- Pause state evidence.
- Rejection messages.
- Accepted change-revision evidence.

## Final Reporting Template

Claude should finish with a compact report containing:

1. Overall verdict: pass or fail.
2. Passed cases.
3. Failed cases.
4. Blocked cases.
5. The highest-severity defects found.
6. Any mismatches between implementation and `01-product-spec.md`.
