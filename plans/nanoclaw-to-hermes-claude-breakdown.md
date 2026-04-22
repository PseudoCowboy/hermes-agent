# NanoClaw to Hermes Detailed Breakdown for Claude

Date: 2026-04-09
Scope: detailed execution plan for coding work

## Recommendation

Yes. This is a good split.

Claude should do the code work, but only against a narrow target:

- do **not** chase full NanoClaw Discord parity
- do **not** rebuild multi-bot routing first
- do **not** build a custom plugin command framework first

Instead, target a Hermes-native repo workflow that uses **Hermes command style** and **skill slash commands**.

That makes the migration materially simpler.

## Core Design Decision

Use this operator surface:

- Hermes built-in commands where they already fit
- Hermes **skill slash commands** for workflow entrypoints
- a small deterministic **workflow toolset** underneath

Do **not** use these as the first implementation target:

- NanoClaw `!command` compatibility
- Discord-specific workflow commands in gateway core
- plugin slash-command infrastructure

Why this is the right target:

- `agent/skill_commands.py` already exposes skills as slash commands in CLI and gateway
- existing Hermes skills already cover planning, delegation, and code review
- the missing piece is deterministic workflow state management, not a new command UX

## Target Product Slice

The first usable migration should support this end-to-end flow:

1. Human invokes `/workflow-create-project`.
2. Hermes scaffolds the NanoClaw-style repo contract under a project folder.
3. Human invokes `/workflow-plan`.
4. Hermes uses plan-writing behavior and saves the result into the agreed project files.
5. Human explicitly approves via `/workflow-approve-plan`.
6. Hermes invokes `/workflow-decompose` to create workstreams, manifest, scope files, and starter task files.
7. Human or Hermes invokes `/workflow-execute-stream` for a stream.
8. Hermes uses delegation/review skills and writes durable state files.
9. Human invokes `/workflow-status` or `/workflow-dashboard` to inspect progress.

This is enough to prove the migration.

## Keep vs Change

### Keep from NanoClaw

- repo-first truth model
- durable plan/task/review artifacts
- explicit owner/reviewer semantics
- human approval before decomposition
- evidence-first review flow
- handoff files and dependency files

### Change for Hermes

- replace `!create_project`, `!plan`, `!approve_plan`, `!decompose`, etc. with Hermes skill commands
- replace orchestrator-specific command handlers with workflow tools + skills
- replace multi-bot assumption with one Hermes control plane plus delegated work
- defer Discord-specific automation until the repo workflow is solid

## Recommended Repo Shape

For the first cut, keep the NanoClaw project layout to reduce migration risk:

```text
groups/shared_project/active/<project-slug>/
  control/
  coordination/
  plans/
  workstreams/
  archive/
```

Do not redesign this into `changes/<id>/...` yet.

That can happen later if desired.

## Code Work Breakdown

### Work Package 1: Workflow Core Library

Goal: centralize the file/state contract so the skills do not manipulate raw paths ad hoc.

Expected files:

- `tools/workflow_tools.py`
- optional helper module if needed, for example `agent/workflow_contract.py` or `tools/workflow_contract.py`
- `tests/tools/test_workflow_tools.py`

Required capabilities:

- project slug/path resolution
- canonical path helpers for `control/`, `coordination/`, `plans/`, `workstreams/`
- read/write helpers for `plan-state.json`, `task-state.json`, `manifest.json`
- validation helpers for required files
- safe initialization that does not overwrite existing work accidentally

Required invariants:

- approved plan required before decomposition
- code streams must have distinct owner and reviewer
- report/design/research streams may omit reviewer only when mode is explicit
- state files must stay parseable

Definition of done:

- all path/state helpers live in one place
- unit tests cover project creation and invalid-state rejection

### Work Package 2: Deterministic Workflow Tools

Goal: give Hermes deterministic tools for the workflow operations that should not rely on free-form prompting.

Recommended tool surface:

- `workflow_create_project`
- `workflow_approve_plan`
- `workflow_decompose`
- `workflow_status`
- `workflow_handoff`
- `workflow_checkpoint`

Optional first-slice tools:

- `workflow_update_task_state`
- `workflow_write_review_report`
- `workflow_dashboard`

Implementation notes:

- all handlers should return JSON strings like other Hermes tools
- add import in `model_tools.py`
- add a new toolset in `toolsets.py`, for example `workflow`
- keep schema descriptions self-contained and do not mention unavailable cross-tools by name

Definition of done:

- tools can create and validate the repo contract without manual shell work
- tests cover happy path and invariant violations

### Work Package 3: Skill Command Surface

Goal: expose the workflow using Hermes-native skill commands instead of custom Discord commands.

Use skills, not plugin commands.

Expected skill set:

- `workflow-create-project`
- `workflow-plan`
- `workflow-approve-plan`
- `workflow-decompose`
- `workflow-execute-stream`
- `workflow-review-stream`
- `workflow-status`
- `workflow-dashboard`

Recommended skill behavior:

- `workflow-create-project`
  - call deterministic create-project tool
  - explain created files and next step
- `workflow-plan`
  - load the planning skill pattern
  - save outputs to `plans/<slug>/plan.md`, `plan-v2.md`, `plan-state.json`
  - never claim planning is complete until files are written
- `workflow-approve-plan`
  - copy/finalize approved plan into `control/approved-plan.md`
  - update `plan-state.json`
- `workflow-decompose`
  - read approved plan
  - create `workstreams/manifest.json` and per-stream files
  - fail if reviewer/acceptance criteria/dependencies are missing
- `workflow-execute-stream`
  - read stream contract
  - invoke implementation-oriented behavior using existing Hermes skills
  - keep task-state honest
- `workflow-review-stream`
  - produce durable `review-report.md`
  - update task state to `approved` or `changes_requested`
- `workflow-status` and `workflow-dashboard`
  - summarize file-backed state only

Implementation note:

- prefer composing existing Hermes skills (`writing-plans`, `subagent-driven-development`, `requesting-code-review`) instead of rewriting their logic

Definition of done:

- the workflow is usable from Hermes skill slash commands in CLI/gateway
- no custom command-registration work is required

### Work Package 4: Stream Execution and Review Discipline

Goal: preserve NanoClaw's workflow reliability without needing NanoClaw's watcher runtime first.

Required behaviors:

- one active owner per stream task
- code work does not self-approve
- review produces `review-report.md`
- task-state transitions are validated
- evidence locations are explicit

Recommended implementation:

- keep transitions deterministic in tools/helpers
- let skills handle reasoning and narrative
- let file artifacts remain the source of truth

Minimal acceptable task lifecycle for first cut:

```text
pending -> in_progress -> implemented -> in_review -> approved
                     \-> changes_requested -> in_progress
```

Definition of done:

- one stream can go from decomposition to implementation to review using Hermes only

### Work Package 5: NanoClaw Artifact Migrator

Goal: import durable content and archive NanoClaw-specific runtime state.

Expected files:

- `optional-skills/migration/nanoclaw-migration/scripts/nanoclaw_to_hermes.py`
- tests for the migrator
- optional CLI entrypoint if Claude decides to expose it immediately

Import directly:

- `AGENTS.md`
- workflow docs
- reusable templates and specs
- role instructions that can become Hermes skills or project files
- allowlisted environment/config values that map cleanly

Archive only:

- multi-bot Discord tokens/bindings
- watcher runtime state
- NanoClaw container-runner specifics
- NanoClaw-only command metadata

Implementation rule:

- follow the OpenClaw migrator style exactly where possible: import portable items, archive unmapped items, produce a report

Definition of done:

- dry-run report clearly shows migrated vs archived items

### Work Package 6: Optional Notifications Layer

Goal: add convenience messaging after the repo workflow works.

This is optional and should not block the first usable migration.

If implemented, use:

- existing gateway support
- `send_message` only for notifications
- file-backed truth for actual workflow state

Do not implement watcher-driven Discord automation until the core repo workflow is stable.

## Suggested Delivery Order for Claude

### PR 1: Workflow core + create/approve/status tools

Deliver:

- core path/state helpers
- `workflow_create_project`
- `workflow_approve_plan`
- `workflow_status`
- tests

Why first:

- lowest ambiguity
- establishes the repo contract
- gives immediate operator value

### PR 2: Decompose + handoff + checkpoint tools

Deliver:

- `workflow_decompose`
- `workflow_handoff`
- `workflow_checkpoint`
- task/manifest validation
- tests

Why second:

- completes the deterministic workflow skeleton

### PR 3: Skill command layer

Deliver:

- workflow skills wired on top of the deterministic tools
- documentation for how to use them

Why third:

- once the deterministic backend exists, skill behavior is easier to keep honest

### PR 4: Execute-stream and review-stream flows

Deliver:

- stream execution skill
- stream review skill
- review report writing
- state transition updates

Why fourth:

- this is the first point where Hermes starts replacing actual NanoClaw workstream execution

### PR 5: Migrator

Deliver:

- NanoClaw artifact migrator
- migration report output
- docs

Why fifth:

- once the target surfaces exist, the migrator has somewhere stable to import into

## Explicit Deferrals

Claude should defer these unless asked explicitly:

- multi-bot Discord identity parity
- automatic filesystem watchers
- Discord-specific orchestration commands
- gateway-core command extension work
- full worktree automation beyond what is needed for one stream
- converting the project layout away from `groups/shared_project/active/`

## Acceptance Criteria for the Whole Effort

The migration is good enough when all of these are true:

- Hermes can scaffold a NanoClaw-style project structure from a skill command
- Hermes can write and approve a file-backed plan
- Hermes can decompose an approved plan into durable workstream artifacts
- Hermes can execute at least one stream and write task state honestly
- Hermes can review that stream and emit `review-report.md`
- status/dashboard output comes from files, not chat reconstruction
- NanoClaw docs and roles can be imported or adapted without preserving NanoClaw runtime semantics

## Review Strategy

Recommended split:

- Claude writes code in small PR-sized chunks
- Codex reviews architecture, invariants, and migration shape after each chunk

This reduces the chance that Claude builds the wrong abstraction too far before correction.

## Suggested Prompt for Claude

Use something close to this:

```text
Implement the NanoClaw-to-Hermes migration in small steps.

Constraints:
- Do not preserve NanoClaw !command compatibility.
- Use Hermes skill slash commands as the operator surface.
- Put deterministic repo/state operations into a new workflow toolset.
- Keep the NanoClaw-style project layout under groups/shared_project/active/<slug>/ for now.
- Reuse Hermes planning/review/delegation skills where possible.
- Do not build multi-bot Discord parity.
- Do not build plugin command infrastructure.

First task:
- Implement workflow core helpers plus workflow_create_project, workflow_approve_plan, and workflow_status.
- Add tests.
- Keep changes minimal and well-scoped.
```

## Bottom Line

If slash-command compatibility is not important, the right implementation path is:

- skills as the command surface
- tools as the deterministic state layer
- repo files as source of truth
- migration of artifacts first
- Discord automation later, if still needed

That is a tractable coding plan for Claude.
