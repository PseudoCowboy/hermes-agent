# NanoClaw to Hermes Migration Feasibility

Date: 2026-04-09
NanoClaw repo: `/Users/jiangzejia/code/analysis/harness/nanoclaw`
Hermes repo: `/Users/jiangzejia/code/analysis/hermes-agent`

## Short Answer

Yes, migration is possible, but not as a straight import.

What migrates cleanly:

- Discord channel connectivity at a basic platform level
- Repo-first project artifacts and workflow documents
- Per-agent instructions and role definitions as Hermes skills, personalities, or project context files
- Scheduled tasks conceptually
- Cross-session memory and search
- Some cross-platform messaging patterns

What does not currently have a Hermes-native equivalent:

- NanoClaw's Discord-specific multi-agent control plane
- Project/workstream orchestration commands like `!create_project`, `!plan`, `!approve_plan`, `!decompose`, `!handoff`, `!checkpoint`
- Persisted workstream state machine driven by `task-state.json`
- Stream watchers that trigger implementer/reviewer agents automatically
- Per-workstream git worktree lifecycle managed by the harness
- Agent roster modeled as separate long-lived Discord bots with role-specific routing rules

Conclusion: migrating NanoClaw into Hermes is realistic if the goal is to preserve the workflow model and artifacts. It is not realistic as a drop-in runtime migration where Hermes already behaves like NanoClaw's Discord orchestration system.

## NanoClaw Ground Truth

NanoClaw is a TypeScript/Node system built around:

- A single host orchestrator in `src/index.ts`
- Channel self-registration and a Discord command layer
- Containerized Claude Agent SDK execution
- Repo/file-first project coordination under `groups/shared_project/active/<project>/`
- A Discord multi-agent operating model with Iris, Athena, Hermes, Atlas, Apollo, and Argus
- A persisted workstream/review state machine in files plus SQLite-backed orchestration metadata

The multi-agent workflow docs are consistent with the broad design direction:

- `docs/MULTI-AGENT-WORKFLOW-INSTRUCTIONS.md`
- `docs/MULTI-AGENT-PROJECT-DEVELOPMENT-SPEC.md`

The strongest implemented part is the execution/review loop, not the planning debate loop.

## Hermes Ground Truth

Hermes already has:

- Discord as a supported gateway platform
- A generic messaging gateway with many platforms
- Subagent delegation via `delegate_task`
- Session branching via `/branch`
- Planning/review oriented skills such as `writing-plans`, `subagent-driven-development`, and `requesting-code-review`
- Cron scheduling, memory, session search, plugins, and MCP

Hermes does not currently expose a NanoClaw-like Discord orchestration layer. Its Discord adapter is a standard chat platform adapter, not a project factory runtime.

The existing OpenClaw migration path is broad for user data, but it explicitly archives complex agent/gateway/multi-agent configuration for manual recreation rather than recreating the runtime behavior automatically.

## Feasibility Assessment

### 1. Basic user/state migration

High feasibility.

Likely portable into Hermes with a custom NanoClaw migrator or by extending the OpenClaw migrator:

- Discord tokens / allowlists
- Workspace path defaults
- Per-agent instructions and memory docs
- Scheduled task definitions as archive + manual recreation
- Repo-local specs, plans, ADRs, and workflow docs

### 2. Repo-first workflow migration

High feasibility.

NanoClaw's repo-first workflow is actually a strong fit for Hermes, because Hermes can already consume:

- `AGENTS.md`
- project-local docs
- skills
- task plans
- delegated subtasks
- code-review skills

The structural contract from NanoClaw's spec can be preserved almost unchanged inside a Hermes-managed repo.

### 3. Discord multi-agent workflow migration

Medium feasibility, but only with new Hermes work.

To preserve the NanoClaw Discord workflow, Hermes would need custom additions such as:

- a Discord project/orchestration command layer
- persistent project/workstream state files plus watcher logic
- a mapping from stream state changes to delegated Hermes subagents or external workers
- reviewer routing and review-gate transitions
- branch/worktree management for isolated stream execution

Hermes has pieces of this, but not the assembled control plane.

### 4. Runtime model equivalence

Low feasibility as a direct translation.

NanoClaw's runtime assumes:

- separate named agents with explicit Discord routing semantics
- file and SQLite orchestration state driving automation
- host-managed project worktrees per workstream
- Discord commands as the main operator surface

Hermes instead assumes:

- one primary agent session that may delegate subagents
- platform adapters as transport surfaces
- generic slash commands rather than project-specific Discord workflow commands
- no built-in persisted workstream runtime comparable to NanoClaw's `stream-watcher`

## What the Existing Hermes Migrator Tells Us

Hermes' OpenClaw migration tooling already distinguishes between:

- settings that map cleanly into Hermes
- settings that should be archived for manual review

That is important because it shows the likely right migration shape for NanoClaw too.

The current OpenClaw migrator:

- imports compatible messaging/settings/env values
- imports memory, skills, workspace instructions, MCP, some model config
- archives multi-agent lists, bindings, gateway config, advanced channel config, hooks, plugins, and other complex runtime settings

That means Hermes already has the right migration philosophy for NanoClaw: import the durable user/repo state, archive the harness-specific orchestration state, then rebuild the missing runtime intentionally.

## Recommended Migration Strategy

### Recommended target

Migrate NanoClaw to Hermes in two layers:

1. Migrate the repo-first project/workflow contract first.
2. Rebuild only the Discord orchestration pieces that are still valuable after the move.

### Phase 1: Portable artifacts

Move or preserve these first:

- `AGENTS.md`
- `ARCHITECTURE.md`
- `docs/MULTI-AGENT-WORKFLOW-INSTRUCTIONS.md`
- `docs/MULTI-AGENT-PROJECT-DEVELOPMENT-SPEC.md`
- project artifact layout under `groups/shared_project/active/<project>/...` or an adapted equivalent
- role definitions for Iris / Athena / Hermes / Atlas / Apollo / Argus

In Hermes, these can become:

- project context files
- skills
- slash-command conventions
- plan/review task templates

### Phase 2: Process emulation with existing Hermes features

Approximate the NanoClaw workflow using Hermes features already present:

- `writing-plans` for plan generation
- `subagent-driven-development` for implementation delegation
- `requesting-code-review` for reviewer separation
- `delegate_task` for bounded worker roles
- `/branch` plus git worktrees or repo conventions for isolated implementation paths
- cron + send_message for periodic reporting / notifications

This will not be Discord-native NanoClaw parity, but it gets much of the development process back quickly.

### Phase 3: Optional Hermes-native orchestration plugin

If you want true NanoClaw-style Discord workflow inside Hermes, build it as a Hermes plugin/toolset instead of trying to force it into the generic gateway core.

Likely pieces:

- a plugin-defined Discord workflow command set
- persistent project/workstream metadata
- watchers for `task-state.json`, `handoffs.md`, and dependency files
- delegated subagent runners bound to role definitions
- review gate transitions and escalation rules

This is feasible, but it is product work, not migration glue.

## Main Risks

- The biggest loss in a naive migration is the Discord control plane behavior, not the agent content.
- NanoClaw's named-agent model does not map 1:1 to Hermes' primary-agent-plus-subagents model.
- NanoClaw uses real harness state and watchers; Hermes currently uses skills and delegation patterns rather than a persisted project factory runtime.
- Some NanoClaw docs already drift from implementation. A migration should treat current code and artifact patterns as ground truth, not every document literally.

## Bottom Line

It is possible to migrate NanoClaw to Hermes if you define the migration correctly.

If "migrate" means:

- keep the repo-first development contract
- preserve the planning/workstream/review artifact model
- port user/channel/state where it maps cleanly
- rebuild the Discord orchestration layer on top of Hermes intentionally

then the answer is yes.

If "migrate" means:

- point Hermes at the NanoClaw repo and immediately get the same multi-agent Discord workflow

then the answer is no.

The practical recommendation is:

- migrate the portable artifacts and settings first
- use Hermes' existing planning, delegation, review, memory, and gateway features as the first working baseline
- only then decide whether the NanoClaw Discord orchestration runtime is important enough to rebuild as a Hermes plugin or toolset
