# NanoClaw to Hermes Migration Plan

Date: 2026-04-09
Hermes repo: `/Users/jiangzejia/code/analysis/hermes-agent`
NanoClaw repo: `/Users/jiangzejia/code/analysis/harness/nanoclaw`

## Inputs Compared

This plan reconciles four inputs:

- `plans/hermes-agent-baseline-analysis.md`
- `plans/nanoclaw-to-hermes-migration-feasibility.md`
- Claude memory: `project_hermes_analysis.md`
- Claude memory: `project_migration_analysis.md`

## Comparison Summary

### Where both analyses agree

- NanoClaw and Hermes are not runtime-equivalent systems.
- Durable repo artifacts migrate much more cleanly than orchestration behavior.
- NanoClaw's Discord multi-agent control plane is the hardest part to preserve.
- Hermes' existing OpenClaw migration path is the right precedent: import what maps cleanly, archive what does not, then rebuild intentionally.

### Where the analyses differ

Claude's conclusion is more conservative:

- keep NanoClaw as the orchestrator
- use Hermes as the worker/agent engine inside the existing workflow

My earlier conclusion is broader:

- a full Hermes-native migration is possible
- but only if the Discord orchestration layer is treated as new product work, not as a config import

### Reconciled recommendation

Treat this as a phased migration with two valid stopping points:

1. **Recommended first target:** keep the NanoClaw orchestration model, replace the worker runtime with Hermes.
2. **Optional final target:** rebuild the NanoClaw workflow natively inside Hermes after the hybrid path is stable.

This preserves the Discord workflow that matters most, while keeping open a path to a cleaner Hermes-native system later.

## Decision

Do **not** attempt a big-bang "NanoClaw fully becomes Hermes" cutover.

Use a phased plan:

- Phase 1: import portable artifacts into Hermes
- Phase 2: run Hermes inside the NanoClaw workflow
- Phase 3: add the missing Hermes command/plugin foundation
- Phase 4: rebuild the Discord workflow on Hermes only if Phase 2 proves insufficient

## Migration Classification

| NanoClaw element | Hermes target | Strategy | Notes |
|---|---|---|---|
| `AGENTS.md`, workflow docs, specs, plans | Repo files, skills, project context | Import directly | Strong fit with Hermes repo-first workflow |
| `groups/shared_project/active/<slug>/...` artifacts | Keep same layout initially | Preserve | Avoid layout churn during migration |
| Role definitions for Athena/Hermes/Atlas/Apollo/Argus | Skills, role presets, or profile-specific instructions | Adapt | Roles matter more than agent names |
| `plan-state.json`, `task-state.json`, `manifest.json`, `review-report.md` | Repo-backed workflow state | Preserve and validate | Repo should remain the source of truth |
| Discord command surface (`!plan`, `!decompose`, `!approve_plan`, etc.) | Hermes slash commands or command plugin | Rebuild | No drop-in Hermes equivalent today |
| `stream-watcher.ts` and workspace watchers | Hermes watcher service/plugin | Rebuild | This is the main missing control plane |
| Per-stream worktree lifecycle | Hermes helper tool/plugin plus git worktrees | Adapt/rebuild | Hermes has terminals; not the NanoClaw worktree harness |
| Multi-bot Discord routing with per-agent tokens | Archive first | Manual redesign | Not a clean 1:1 mapping to Hermes' primary-agent model |
| Container runner and SDK glue | Hermes runtime in container or direct Hermes execution | Replace in hybrid path | Best first runtime swap point |
| Cron/maintenance drift checks | Hermes cron plus validation commands | Adapt | Concept transfers well |

## Important Constraints Discovered In Hermes

### 1. The OpenClaw migrator already encodes the right migration philosophy

`optional-skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py` imports portable state and archives complex runtime config. NanoClaw should follow the same pattern instead of forcing a full behavioral import.

### 2. Hermes plugin tools are real, but plugin slash commands are not fully implemented

`gateway/run.py` and `cli.py` both try to dispatch plugin commands via `get_plugin_command_handler(...)`, but `hermes_cli/plugins.py` does not currently implement that surface, and `tests/hermes_cli/test_plugins.py` explicitly notes that plugin command registration was never implemented.

Implication: if you want a NanoClaw-style Discord workflow as a clean Hermes plugin, the command-extension foundation has to be built first.

### 3. Hermes subagents are useful, but they do not replace NanoClaw's orchestrator on their own

`delegate_task` is helpful for planning, implementation, and review, but child agents cannot call `delegate_task`, `memory`, `send_message`, or `execute_code`.

Implication: the parent workflow layer still has to own:

- Discord notifications
- durable state transitions
- watcher logic
- human approval gates
- orchestration recovery after restart

### 4. NanoClaw's multi-bot model does not map cleanly to Hermes v1

NanoClaw's `agents/config.json` uses multiple Discord bot tokens and per-role routing rules. Hermes can support Discord, profiles, and role-specific instructions, but it does not already provide NanoClaw's multi-bot orchestration semantics.

Implication: do not make multi-bot parity a phase-1 requirement.

## Recommended Architecture Target

### Phase-1 target: Hybrid

Keep these NanoClaw responsibilities for now:

- Discord command entrypoints
- workstream watchers
- persisted workstream/project state
- branch/worktree lifecycle
- explicit reviewer routing

Replace or augment these parts with Hermes:

- planning/review/implementation agent runtime
- role instructions and skills
- memory/search/session handling where useful
- subagent execution inside the worker runtime

This is the lowest-risk way to preserve the workflow the user cares about.

### Phase-2 target: Hermes-native

Only after hybrid stabilization, move these responsibilities into Hermes:

- workflow command registration
- state-machine validators
- watcher daemon
- worktree management helpers
- review gating and dashboard reporting

## Detailed Plan

### Phase 0: Freeze the current workflow contract

Goal: define what must stay behaviorally true across migration.

Actions:

- Treat NanoClaw code, `agents/config.json`, and current workflow docs as the baseline contract.
- Produce a short "migration invariants" checklist from the current repo:
  - repo is source of truth
  - approved plan required before decomposition
  - code streams cannot self-approve
  - restart recovery comes from files, not Discord history
  - handoffs and review reports are durable artifacts
- Mark any doc/code drift explicitly so it does not get migrated as false truth.

Exit criteria:

- one canonical list of invariants exists
- current commands, files, and role semantics are inventoried

### Phase 1: Build a NanoClaw artifact migrator for Hermes

Goal: move portable content first and archive the rest.

Recommended implementation:

- Add a new migrator parallel to the OpenClaw one, for example:
  - `optional-skills/migration/nanoclaw-migration/scripts/nanoclaw_to_hermes.py`
- Add a CLI entrypoint for it, either:
  - extend `hermes claw` with a NanoClaw mode, or
  - add a dedicated `hermes nanoclaw migrate` command

Import directly:

- workflow docs and repo-local plans
- `AGENTS.md`
- role instructions that can become Hermes skills or role presets
- any reusable project templates
- allowlisted environment/config values that map cleanly

Archive only:

- per-agent Discord tokens and multi-bot bindings
- orchestration SQLite/runtime state that assumes NanoClaw watchers
- Discord channel routing rules tied to NanoClaw semantics
- container-runner specifics that Hermes will not consume directly

Deliverables:

- migration report similar to OpenClaw's report format
- archived NanoClaw-only configuration under a clear output directory
- imported skills/docs under Hermes-owned locations

Exit criteria:

- a dry run clearly separates imported vs archived items
- the migration is repeatable and non-destructive

### Phase 2: Run Hermes as the NanoClaw worker engine

Goal: preserve the Discord workflow while replacing the worker runtime.

Recommended approach:

- Keep NanoClaw's Discord command and watcher layer intact.
- Replace the current Claude Agent SDK execution path in the worker/container path with Hermes.
- Feed Hermes role-specific instructions based on the NanoClaw role being invoked.
- Keep the existing repo artifact layout and state-machine files unchanged.

Role mapping guidance:

- Athena/Hermes: Hermes planning skills plus role-specific instructions
- Atlas/Apollo: Hermes implementation workflow plus repo-local instructions
- Argus: Hermes code-review workflow plus explicit review-report output contract

Key engineering rule:

- Hermes should be the execution engine, not the orchestrator, in this phase.

Why this phase matters:

- it preserves the Discord development workflow
- it validates that Hermes can do the actual planning, implementation, and review work
- it avoids rebuilding the control plane before proving the engine swap works

Exit criteria:

- `!plan`, `!approve_plan`, `!decompose`, `!handoff`, and workstream review still function through NanoClaw
- worker outputs are produced by Hermes
- restart/review behavior still follows file-backed state

### Phase 3: Add the missing Hermes command/plugin foundation

Goal: make Hermes capable of owning workflow commands cleanly.

Recommended work order:

1. Implement plugin slash-command registration in Hermes.
2. Add tests for plugin command discovery, help/autocomplete, and gateway dispatch.
3. Decide whether workflow commands should live in:
   - a general plugin, or
   - core `COMMAND_REGISTRY` plus gateway handlers.

Recommendation:

- build the missing plugin command foundation first
- then keep the NanoClaw workflow logic out of Hermes core as much as possible

Why:

- the workflow is specialized
- Hermes core is already large
- plugin isolation will make the migration easier to evolve and easier to back out

Exit criteria:

- a plugin can register a slash command and handle it in both CLI and gateway contexts

### Phase 4: Rebuild the Discord workflow as a Hermes workflow plugin

Goal: move the orchestration layer from NanoClaw into Hermes deliberately.

Recommended plugin responsibilities:

- project creation and workflow commands
- repo-backed state parsing and validation
- watcher loop for workstream/task changes
- review gate transitions
- dashboard/status rendering
- restart recovery from files plus minimal plugin state

Suggested command set for first parity slice:

- `/create-project`
- `/plan`
- `/plan-status`
- `/approve-plan`
- `/decompose`
- `/stream-status`
- `/handoff`
- `/checkpoint`
- `/checkpoints`
- `/doctor-workflow`
- `/dashboard`

Suggested state model:

- keep `plan-state.json`, `task-state.json`, `manifest.json`, and `review-report.md`
- use Hermes-side state only for ephemeral handles, watches, and message routing metadata
- keep the repository as the system of record

Suggested execution model:

- use one control-plane Hermes session/bot to own workflow commands
- use role-specific delegation or role-configured worker execution for implementation/review tasks
- add a helper for worktree allocation instead of depending on ad hoc shell logic everywhere

Important non-goal for first native slice:

- do not try to fully reproduce NanoClaw's multiple long-lived Discord bot identities first

Exit criteria:

- a Hermes-managed workflow can create a project, approve a plan, decompose streams, run at least one code stream through review, and recover after restart from repo files

### Phase 5: Controlled cutover

Goal: remove NanoClaw only after Hermes-native workflow parity is proven.

Actions:

- run one pilot project entirely through the Hermes workflow plugin
- compare artifacts and operator experience against the NanoClaw baseline
- keep rollback simple: NanoClaw remains available until Hermes-native orchestration survives real usage

Cutover criteria:

- human approval and review loops are preserved
- state recovery works after restart
- no critical workflow invariant depends on Discord history
- stream ownership and review routing are honest and inspectable

## Rough Effort Shape

These are rough estimates, not commitments.

| Work item | Size | Comment |
|---|---|---|
| NanoClaw artifact migrator | Medium | Straightforward because OpenClaw precedent exists |
| Hybrid runtime swap | Medium | Main uncertainty is Hermes invocation shape inside NanoClaw |
| Hermes plugin-command foundation | Small to Medium | Bounded but core-touching |
| Hermes-native workflow plugin | Large | Main product work |
| Full multi-bot parity | Extra Large | Not recommended as an early goal |

## Main Risks

- Trying to migrate the Discord control plane as if it were configuration.
- Rewriting file layout and runtime behavior at the same time.
- Treating NanoClaw's named-agent Discord identities as phase-1 requirements.
- Assuming Hermes subagents eliminate the need for watcher/state logic.
- Letting doc drift become migration truth.

## Recommended Next Implementation Order

1. Write the NanoClaw migrator spec and item classification using the OpenClaw migrator format.
2. Prototype Hermes as the worker runtime for one NanoClaw role.
3. Verify that planning, implementation, and review artifacts remain identical or intentionally better.
4. Implement Hermes plugin command registration.
5. Build the Hermes workflow plugin only after the hybrid path is stable.

## Bottom Line

Claude's recommendation is the right first move.

If the most important requirement is preserving the Discord multi-agent development workflow, the best migration path is:

- first move NanoClaw's worker engine to Hermes
- keep NanoClaw's orchestration until Hermes proves it can replace it
- then, only if needed, rebuild the control plane natively inside Hermes using a command/plugin layer plus repo-backed watchers

That gives you a practical migration path now without closing the door on a full Hermes-native future later.
