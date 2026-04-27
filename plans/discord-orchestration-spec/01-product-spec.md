# Product Spec: Discord-Orchestrated Multi-Agent Delivery

## Status

Derived v1 product contract.

## Purpose

The system accepts a software requirement in Discord and drives it through clarification, planning, decomposition, implementation, testing, approval, and archive with minimal operator effort.

The intended operator experience is simple:

1. The operator sends an idea.
2. The system turns that idea into an executable project plan.
3. The system runs multiple workstreams in parallel.
4. The system asks for help only when blocked or when human judgement is required.
5. The system delivers a finished, reviewed project without cross-project leakage.

## Product Goals

- Let a single operator start a project from a requirement message in Discord.
- Keep planning, implementation, review, and operator feedback in Discord-native surfaces.
- Run multiple project streams in parallel.
- Preserve strong operational isolation between projects and streams.
- Recover conservatively from crashes or ambiguous delivery events.

## Non-Goals

- Strong sandbox security against a malicious agent.
- Multi-owner or team collaboration workflows in v1.
- Silent continuation after ambiguous failures.
- Auto-implementation before explicit plan approval.

## Actors

- `Operator`: the human who starts and owns a project.
- `Orchestrator`: the agent responsible for clarification, planning, decomposition, coordination, and project-level state.
- `Implementer`: a stream-bound agent that executes one workstream.
- `Test agent`: a stream-bound or project-bound verifier that checks acceptance criteria.

## Core Concepts

- `Home channel`: the Discord channel where new projects are created.
- `Project`: a scoped orchestration unit created from one requirement.
- `Project category`: the Discord category for a project.
- `Main channel`: the project control channel for plan approval, change requests, and project-level resumes.
- `Stream channel`: a channel dedicated to one implementation stream.
- `Review channel`: the channel used for test results and human-judgement review items.
- `Project slug`: a stable identifier derived from the requirement.
- `Scope ID`: a unique project namespace key.
- `Reviewed revision`: the exact approved implementation revision eligible for merge.

## Normative Requirements

### 1. Project creation and isolation

- The system MUST create a new project only from an explicit new-project command in the home channel.
- The system MUST create one isolated project category per project.
- The project category MUST be visible only to the operator and the bot in v1.
- The system MUST create a main channel immediately.
- The system MUST NOT create stream channels before the plan is approved.
- Projects MUST be isolated in Discord visibility, workflow state, workspace paths, and concurrency control.

### 2. Clarification

- The orchestrator MAY ask zero or more clarifying questions before drafting a plan.
- The clarification phase MUST be bounded. The current v1 policy is at most two rounds.
- A plain operator message MUST be transcribed but MUST NOT advance a waiting clarification unless it qualifies as an answer to a pending prompt.
- A clarification answer MUST be admitted only when it is bound to the currently pending prompt.
- Admission MUST be one-shot. Once one qualifying answer is admitted, later concurrent answers to the same wait MUST be dropped.
- If the requirement is already clear enough, the orchestrator MAY skip clarification.

### 3. Plan drafting and approval

- The system MUST draft a plan before implementation starts.
- The plan MUST be internally reviewed before operator approval.
- The internal review loop MUST be bounded.
- The plan posted to the operator MUST include explicit actions for approval, revision, or rejection.
- An unapproved plan MUST NEVER trigger implementation.
- Rejection MUST stop the project and clean up project state according to the archive or abandonment policy.

### 4. Plan structure

- A code-delivery plan MUST decompose work into one or more streams.
- Each code stream MUST have:
  - a stable name,
  - an assigned role,
  - acceptance criteria,
  - dependency information.
- Acceptance criteria SHOULD include both automatable checks and human-judgement checks when the work requires both.

### 5. Stream execution

- After plan approval, the system MUST decompose the plan into stream tasks and create stream execution channels.
- Each stream MUST run in its own workspace scope.
- A stream MUST NOT be able to mutate another stream's files or workflow state through normal tool use.
- Streams SHOULD run in parallel when dependencies allow it.
- After each completed task, the stream SHOULD post a concise progress update in its own channel.
- A blocked stream MAY request operator clarification in its own channel.

### 6. Verification and approval

- When a stream reaches implementation completion, the system MUST run a verification pass before merge.
- Automatable criteria MUST be executed and reported with evidence.
- Human-judgement criteria MUST be surfaced for operator approval or rejection.
- The system MUST NOT auto-retry failed verification indefinitely.
- A stream becomes merge-eligible only after explicit approval of its verification outcome.
- Approval MUST bind to the reviewed revision, not to later unreviewed changes.

### 7. Merge and final integration

- Merge operations for the same project MUST be serialized.
- Only reviewed revisions MAY be merged.
- The system MUST run a final integration verification pass after all required stream merges are complete.
- The project MUST NOT become done until the final integration pass succeeds.

### 8. Pause, resume, and crash recovery

- Stream-scoped and project-scoped pauses MUST be distinguished.
- The system MUST persist enough runstate to recover conservatively after a crash.
- On ambiguous delivery or ambiguous execution outcomes, the system MUST pause rather than assume success.
- Resume commands MUST target either a project or a stream unambiguously.
- On restart, the system MUST rehydrate active work conservatively and MUST NOT destroy unknown work automatically.

### 9. Change requests after approval

- The operator MAY request a plan change after the initial plan is approved.
- A change request MUST be rejected while the project is still active or paused.
- A change request MAY be accepted only when the project is idle.
- Accepted changes MUST go through a new plan revision and approval cycle.
- Previously completed streams SHOULD remain intact unless the new plan changes them.

### 10. Archive and cleanup

- The system MUST support explicit project archival.
- Archive MUST move project state out of the active area.
- Archive MUST remove active stream workspaces.
- Archive SHOULD remove project-specific Discord topology in v1.

### 11. Safety caps

- The system MUST enforce a maximum number of concurrent active projects.
- The system MUST enforce stream safety caps for excessive turn count and excessive wall-clock duration.
- Hitting a safety cap MUST pause the affected stream and require explicit operator resume.

## Required Invariants

- No cross-project message bleed.
- No cross-project workflow-state collision.
- No cross-stream file mutation through normal tool paths.
- No implementation before plan approval.
- No merge of unreviewed revisions.
- No destructive cleanup of unknown work on startup.

## External Commands And Meanings

- `!new <requirement>`: create a new project from a requirement.
- `approve`: approve the current plan in the main channel.
- `changes: <feedback>`: request plan revision.
- `reject`: reject the plan and stop the project.
- `!continue <project-or-stream>`: resume a paused project or stream.
- `!change <description>`: request a post-approval change when the project is idle.
- `!archive [--keep-branch]`: archive a completed or abandoned project.

## Success Definition

The feature is successful when an operator can send an idea in Discord and the system can carry that idea through clarification, planning, implementation, verification, and archival while preserving project isolation and conservative recovery semantics.
