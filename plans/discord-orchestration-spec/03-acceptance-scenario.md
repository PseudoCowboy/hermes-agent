# Acceptance Scenario: Discord Orchestration Bring-Up

## Purpose

This is the end-to-end acceptance scenario for claiming Discord orchestration v1 works.

Pass condition:

- Every required check in this document passes in one continuous bring-up run.
- Failures discovered during scripted crash injection count as failures unless the documented recovery behavior works.

## Scenario Summary

A solo operator drives two Discord-native projects through the orchestration system.

- Project A is the primary scenario.
- Project B is started mid-flight to prove isolation.

## Project A

Home-channel trigger:

```text
!new build a Chrome extension that shows a Chinese translation when I double-click an English word. Dictionary should be user-editable, offline, no network calls.
```

## Project B

Home-channel trigger:

```text
!new build a hello-world flask app on port 5000
```

Start Project B while Project A still has at least one active stream.

## Acceptance Checks

### A. Project creation and topology

- [ ] A1. Project A creates an owner-only category using a slug derived from the requirement.
- [ ] A2. `#{slug}-main` is created immediately.
- [ ] A3. No stream channels are created before plan approval.
- [ ] A4. The orchestrator posts the next expected action in `#{slug}-main`.
- [ ] A5. Project B is isolated in a separate category with no visible channel leakage.

### B. Clarification behavior

- [ ] B1. The orchestrator asks zero to five clarifying questions across no more than two rounds.
- [ ] B2. A plain owner message in `#{slug}-main` is transcribed but does not advance a pending clarification.
- [ ] B3. A native reply to the pending prompt, or an `@bot` mention while the prompt is pending, advances the clarification.
- [ ] B4. Clarification admission is one-shot; a second qualifying reply to the same wait is dropped.
- [ ] B5. If the requirement is clear enough, the orchestrator may skip clarification and proceed directly to planning.

### C. Plan draft and approval loop

- [ ] C1. The orchestrator drafts a plan and runs an internal review loop before the operator sees it.
- [ ] C2. The posted plan includes explicit `approve`, `changes: <feedback>`, and `reject` instructions.
- [ ] C3. For Project A, the final plan contains at least three code streams.
- [ ] C4. For Project A, the plan covers at minimum: MV3 manifest scaffold, content-script translation interaction, popup dictionary editor, and local storage.
- [ ] C5. The plan includes both automatable and human-judgement acceptance criteria.
- [ ] C6. No implementation begins before plan approval.

### D. Decomposition and stream provisioning

- [ ] D1. `approve` triggers plan approval, decomposition, and task synchronization.
- [ ] D2. A project integration workspace exists.
- [ ] D3. One workspace exists per stream.
- [ ] D4. One stream channel exists per stream, plus one review channel.
- [ ] D5. Stream model routing follows the approved stream role assignment.
- [ ] D6. Stream tool access is rooted to each stream's own workspace.

### E. Per-stream execution

- [ ] E1. Each stream writes only inside its own workspace path.
- [ ] E2. After each implemented task, the stream posts a concise progress update in its own channel.
- [ ] E3. At least one stream asks for clarification in its own channel.
- [ ] E4. An answered stream clarification unblocks within one interaction roundtrip.
- [ ] E5. A timed-out clarification pauses only the affected stream and requires `!continue <stream>`.
- [ ] E6. Streams cannot normally read or edit sibling stream files through allowed tool paths.

### F. Per-stream verification

- [ ] F1. When a stream reaches implementation complete, a fresh test agent verifies that stream in the stream's own workspace.
- [ ] F2. Automatable criteria execute with evidence posted to the review channel.
- [ ] F3. Human-judgement criteria are posted separately for operator reaction.
- [ ] F4. Automated verification failure records `changes_requested` and does not auto-retry indefinitely.
- [ ] F5. Approved verification binds merge eligibility to the reviewed revision, not to later unreviewed changes.

### G. Merge and integration

- [ ] G1. Approved stream merges are serialized per project.
- [ ] G2. Project runstate records merge progress atomically enough for crash recovery.
- [ ] G3. A forced merge conflict pauses the project and requires `!continue <slug>` after manual resolution or explicit abort.
- [ ] G4. After all required stream merges, a final integration verification pass runs in the integration workspace.
- [ ] G5. The project becomes `done` only after the final integration pass succeeds.

### H. Archive

- [ ] H1. `!archive` moves the project state from active storage to archive storage.
- [ ] H2. Stream and integration workspaces are removed from the active worktree area.
- [ ] H3. Project-specific Discord topology is deleted.

### I. Multi-project isolation

- [ ] I1. Project B gets a separate category and separate main/stream/review channels.
- [ ] I2. Project B uses a different scoped workflow-state root than Project A.
- [ ] I3. Project B uses a different worktree root than Project A.
- [ ] I4. Same-slug collisions across different scopes do not contend on project locks.
- [ ] I5. Both projects can wait for clarification simultaneously without cross-talk.

### J. Crash recovery

Inject bot crashes deliberately at the following points:

- [ ] J1. Crash while a stream is awaiting clarification. On restart, recovery either rehydrates the wait correctly or pauses conservatively if delivery was indeterminate.
- [ ] J2. Crash mid-merge. On restart, the system inspects git state and either reconciles the finished merge, pauses for manual resolution, or requeues the reviewed revision safely.
- [ ] J3. Crash during final integration verification. On restart, the system pauses conservatively and does not silently auto-rerun.
- [ ] J4. Unknown or corrupt worktree state on startup is logged and not destructively cleaned without explicit operator action.

### K. Safety caps

- [ ] K1. A stream that exceeds the turn cap pauses and requires explicit resume.
- [ ] K2. A stream that exceeds the wall-clock cap pauses and requires explicit resume.
- [ ] K3. A sixth concurrent project is rejected while five are already active.

### L. Post-approval change handling

- [ ] L1. `!change <description>` is rejected while streams are still active.
- [ ] L2. `!change` is rejected while the project is paused.
- [ ] L3. Once the project is idle, `!change` creates a new reviewed plan revision.
- [ ] L4. After an approved change, only new or changed streams are reactivated.

### M. Development Loop (Codex ↔ Claude ↔ Chrome)

Each phase MUST follow the codex-spec → claude-impl → codex-review → chrome-verify loop.

- [ ] M1. For each phase, an OpenAI Codex development spec exists in `plans/` with the phase name and grounded references to current code paths in `/Users/jiangzejia/code/analysis/hermes-agent`.
- [ ] M2. For each phase, an OpenAI Codex implementation task breakdown exists, is numbered, and each task names concrete files.
- [ ] M3. The implementation diff is reviewable as one logical unit on a single branch, optionally squashed.
- [ ] M4. OpenAI Codex review output for the diff is captured at a known path, and Critical/High findings are either resolved or explicitly deferred with rationale.
- [ ] M5. A chrome-devtools MCP verification run drives the Discord web app through the acceptance scenario above and captures snapshots of main and stream channels.

## Verdict

The bring-up is a pass only if every required checkbox above is satisfied and the resulting evidence shows:

- no cross-project leakage,
- no implementation before plan approval,
- no merge of unreviewed content,
- conservative crash recovery,
- the development loop in §M is followed for every phase,
- successful end-to-end completion from requirement to archive.
