# Bring-up Test: Discord Multi-Agent Orchestration (word-translator)

**Purpose:** end-to-end acceptance criteria the system MUST satisfy when `plans/discord-orchestration-design.md` (P1–P8) is fully implemented. Run this as the P8 real-Discord bring-up.

**Branch:** `discord-orchestration-design`
**Target design doc:** `plans/discord-orchestration-design.md`
**Reference artifact:** `groups/shared_project/active/chromewordtranslator/artifacts/extension/` (a pre-existing Word Translator Chrome extension scaffold — used as the *exemplar* of what the orchestration should be able to produce from scratch).

---

## Scenario

A solo operator (project owner) drives two parallel Discord-native projects through the orchestrator. Project A is the primary scenario; project B is started mid-flight to verify isolation.

### Project A — Chrome extension: Word Translator (EN → ZH)

**Trigger (in `#hermes-home`):**
```
!new build a Chrome extension that shows a Chinese translation when I double-click an English word. Dictionary should be user-editable, offline, no network calls.
```

### Project B — control project for isolation (in `#hermes-home`):
```
!new build a hello-world flask app on port 5000
```
Started *while Project A still has at least one stream running*.

---

## Acceptance criteria (ordered; must all pass)

### 1. Project creation & channel topology

- [ ] **A1.** Orchestrator creates a Discord category whose name is a slugified form of the requirement (e.g. `word-translator`); visibility is owner-only (`@everyone` denied `view_channel`; bot + owner granted `view_channel` + `send_messages`).
- [ ] **A2.** `#{slug}-main` is auto-created; additional channels are deferred until after plan approval.
- [ ] **A3.** The orchestrator posts in `#{slug}-main` introducing itself and stating the next step (clarification or plan).
- [ ] **A4.** No channel from Project B is visible inside Project A's category and vice versa.

### 2. Clarification round (in `#{slug}-main`)

- [ ] **B1.** Orchestrator posts 0–5 clarifying questions (≤ 2 rounds total) — e.g. *"Should the popup support import/export?"*, *"Offline-only — do you want the extension to ship with a seed dictionary?"*, *"Should it block the tooltip for specific words (no-translation flag)?"*.
- [ ] **B2.** A plain owner message (no `@bot`, no reply) in `#{slug}-main` is transcribed but does NOT advance the orchestrator past clarification.
- [ ] **B3.** A native reply to the specific clarification prompt (or `@bot`-mention while a prompt is pending) unblocks the orchestrator. Admission is one-shot: a second qualifying reply arriving before the orchestrator dequeues is dropped.
- [ ] **B4.** If the requirement is unambiguous (operator discretion), orchestrator may skip clarification and go straight to plan.

### 3. Plan draft + Codex plan-review loop

- [ ] **C1.** Orchestrator drafts a plan via `workflow_save_plan (state=under_review)`. The plan defines ≥ 3 streams, each with `name`, `completionMode="code"`, `agentRole ∈ {frontend, backend}`, `acceptanceCriteria`, and `dependencies`.
- [ ] **C2.** Codex (`gpt-5.4` xhigh) reviews the draft before the user sees it. Up to 2 revision rounds; the 2nd-round remaining issues (if any) are appended as a `plan-reviewer notes` block when the plan posts.
- [ ] **C3.** Final plan is posted in `#{slug}-main` with explicit instructions: `approve`, `changes: <feedback>`, or `reject`.
- [ ] **C4.** Plan covers (at minimum): manifest V3 scaffold, content script (double-click handler + tooltip), popup (HTML/CSS/JS dictionary editor), storage (chrome.storage.local). Acceptance criteria include at least one human-judgement criterion (e.g. *"tooltip looks …"*) and at least one automatable criterion (e.g. *"manifest.json validates as V3"*).

### 4. Decomposition → per-stream channels + worktrees

On operator `approve` in `#{slug}-main`:

- [ ] **D1.** `workflow_approve_plan` → `workflow_decompose` → `workflow_sync_tasks`.
- [ ] **D2.** `.worktrees/<category-id>/<slug>/_integration/` exists on branch `project/<category-id>/<slug>` based on `main`.
- [ ] **D3.** For each stream `S`, `.worktrees/<category-id>/<slug>/S/` exists on branch `project/<category-id>/<slug>/S`.
- [ ] **D4.** One `#{slug}-{stream}` channel per stream and one `#{slug}-review` channel are created.
- [ ] **D5.** Stream implementers spawn with the correct model: `agentRole=frontend` → Gemini `gemini-3.1-pro`; `agentRole=backend` → Claude `claude-opus-4-7` (max effort). No auto-selection.
- [ ] **D6.** Each implementer's `SandboxedToolset` is rooted at its own stream worktree; a forced escape attempt from the terminal tool (e.g. `cat /etc/passwd`) is rejected or contained per the §3 non-goal note (documented "mistake resistance, not adversarial boundary").

### 5. Per-stream implementation

- [ ] **E1.** Each stream writes files ONLY under its own `.worktrees/<category-id>/<slug>/<stream>/artifacts/…` path (verified by scanning git status of each worktree — no cross-stream diffs).
- [ ] **E2.** Every `workflow_checkpoint(..., new_status='implemented')` is followed by a `discord_post` one-liner in that stream's channel listing task ID, files touched (+adds / −dels), and next task title.
- [ ] **E3.** At least one implementer uses `discord_request_clarification` (e.g. *"should the popup support import/export?"*). The owner replies via native reply; the waiter unblocks within one roundtrip.
- [ ] **E4.** `discord_request_clarification` times out correctly after 15 minutes of no reply → stream transitions to `paused_clarify` → `!continue <stream>` resumes it.
- [ ] **E5.** No stream can see another stream's files via `read`/`edit` (path-allowlist rejection).
- [ ] **E6.** Each stream runs to `implemented` state for all its tasks without the orchestrator merging anything yet.

### 6. Per-stream test pass (pre-merge, in stream's own worktree)

- [ ] **F1.** When a stream reaches `implemented`, a fresh Codex (`gpt-5.4` xhigh) test agent spawns bound to that stream's worktree and `#{slug}-review`.
- [ ] **F2.** Automatable criteria execute in-worktree (pytest / node / jest / static file checks as appropriate) and pass/fail is posted with evidence in `#{slug}-review`.
- [ ] **F3.** Human-judgement criteria are posted with the 🔶 marker in `#{slug}-review` and wait for owner ✅/❌ reaction. Other criteria are not blocked.
- [ ] **F4.** On any automated failure, test agent calls `workflow_review_task(verdict="changes_requested", …)`, @-mentions the owner with evidence, and does NOT auto-retry.
- [ ] **F5.** When all criteria are ✅ or auto-green, `workflow_review_task(verdict="approved", …)` is recorded.

### 7. Merge-on-approve + final integration test

- [ ] **G1.** For each `approved` stream, the orchestrator holds the per-project `asyncio.Lock` and runs `git merge --no-ff project/<category-id>/<slug>/<stream>` inside `_integration/`. Two near-simultaneous approvals serialize — no git index race, no partial merges.
- [ ] **G2.** `project_runstate.json` is updated atomically before and after the merge (`phase="merging"` → `phase="streams_running"` with updated `merged_streams`).
- [ ] **G3.** A forced merge conflict (simulated by pre-conflict seed on main) pauses the project with `phase="paused_merge_conflict"`. `!continue <slug>` resumes after manual resolution in `_integration/`.
- [ ] **G4.** When all streams merged, a final integration test pass spawns in `_integration/` (fresh Codex), posts verdicts in `#{slug}-review`.
- [ ] **G5.** On all-green integration, project transitions to `phase="done"` and orchestrator posts summary in `#{slug}-main` awaiting `!archive` or ✅ reaction.

### 8. Archive

- [ ] **H1.** `!archive` moves `groups/shared_project/<category-id>/active/<slug>/` → `archive/<category-id>-<timestamp>/`.
- [ ] **H2.** All worktrees under `.worktrees/<category-id>/<slug>/` are removed via `git worktree remove`; all `project/<category-id>/<slug>/…` branches are deleted (unless `!archive --keep-branch`).
- [ ] **H3.** Discord category and all its channels are deleted.

### 9. Isolation verified via Project B

Running simultaneously with Project A (at least during steps E–G):

- [ ] **I1.** Project B sees a separate category; its channels never appear in Project A's sidebar.
- [ ] **I2.** Project B's workflow state lives under `groups/shared_project/<B-category-id>/active/<B-slug>/` — zero path overlap with A.
- [ ] **I3.** Project B's git worktrees are under `.worktrees/<B-category-id>/<B-slug>/…` — zero path overlap, independent branches.
- [ ] **I4.** `_project_lock` uses `(scope_id, slug)` tuple — same slug across the two projects never contends.
- [ ] **I5.** Both projects can be in `#{slug}-main` clarification simultaneously without cross-talk.

### 10. Crash recovery

Simulated by `kill -9` on the bot at three moments, then restart:

- [ ] **J1.** Kill while a stream is awaiting clarification → restart rehydrates the waiter, backfills owner replies that arrived via `channel.history(after=prompt_message_id)`, dispatches them if qualified.
- [ ] **J2.** Kill mid-merge in `_integration/` → restart inspects `git status` + `git log --merges`; if the merge landed pre-crash, runstate is reconciled without re-running the merge; if aborted, project pauses with `phase="paused_merge_conflict"` requiring `!continue <slug>`.
- [ ] **J3.** Kill during final integration test → restart pauses with `phase="paused_test_fail"` (outcome unknown) and does NOT auto-rerun; owner uses `!continue <slug>` to re-run the final pass.
- [ ] **J4.** Kill while stream worktrees exist but with a corrupt manifest → startup logs mismatches and refuses destructive cleanup; `!cleanup <slug>` is required to remove unrecognized worktrees.

### 11. Safety caps

- [ ] **K1.** A stream that exceeds 60 LLM turns → `paused_turn_cap` → requires `!continue <stream>`; counters reset from scratch on resume.
- [ ] **K2.** A stream that exceeds 2h wall-clock → `paused_wall_clock` → requires `!continue <stream>`.
- [ ] **K3.** A 6th concurrent project via `!new` is rejected while 5 are already active.

### 12. Post-approval change

- [ ] **L1.** `!change <description>` while any stream is still running is rejected with the documented error message — original plan continues untouched.
- [ ] **L2.** `!change` while the project is paused in `paused_*` state is rejected (not idle).
- [ ] **L3.** Once idle, `!change` produces a `plan-v2.md`, re-runs the Codex plan-review cycle, and on `approve` spawns only streams that are new or changed — completed streams stay archived in place.

---

## Verdict

The bring-up is a **pass** iff every checkbox above is ticked in a single continuous run (with the scripted crash-injections at J1–J3 done deliberately, not by accident). Any box left unchecked blocks declaring P8 complete and must be filed as a follow-up before claiming the orchestration is production-ready for solo-operator use.
