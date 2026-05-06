# Session Handoff: Discord Orchestration Design Review

**Status:** ✅ **Design settled.** 14 Codex review rounds, 30 findings (F1–F30) closed. Round-14 returned **no new High/Medium findings**.

**Date:** 2026-04-24
**Branch:** `discord-orchestration-design` (single clean commit)
**Main design doc:** `plans/discord-orchestration-design.md`
**Bring-up test scenario:** `plans/discord-orchestration-bringup-test.md`
**Latest commit:** `57a822a3 docs(plans): Discord-driven multi-agent workflow orchestration design`

---

## Workflow used

Iterative refinement loop:
1. Edit `plans/discord-orchestration-design.md`.
2. `git commit --amend --no-edit --no-verify` onto the single branch commit.
3. Run Codex review via the `codex-review` skill against `git diff main...HEAD -- plans/discord-orchestration-design.md`.
4. Fix flagged High/Medium issues, repeat.

The branch is intentionally **one clean commit** — always amend, never stack.

## Final iteration log

| Round | Issue | Fix | Status |
|---|---|---|---|
| R1–R5 | F1–F9 (gateway/approval, session model, runstate schema, etc.) | See git history / earlier doc iterations | ✅ closed |
| R6–R7 | F10–F12 (merge recovery, waiter one-shot, !continue disambiguation) | Recovery via `git log --merges --first-parent`; atomic admission clear; `!continue <token>` disjointness | ✅ closed |
| R8 | F13 (stream vs project test-fail conflation), F14 (TOCTOU pre-prompt), F12-residual (stream-name validator) | Split into `paused_stream_test_fail` vs project-scoped; `SENTINEL_PENDING` + `pre_prompt_buffer`; `_validate_streams` rejects `stream_name == slug` | ✅ closed |
| R9 | F15–F17 (adapter guards, reply-key field, merge-conflict bookkeeping) | Bypass `_active_sessions` for long-lived; `MessageEvent.reply_to_message_id`; operator-commits + bot-verifies | ✅ closed |
| R10 | F18–F21 (dispatch authority, admission location, toolset split, merge-abort contradiction) | Runner as single authoritative dispatch; admission on session; `discord-orchestration-admin` vs `-stream`; merge-state preserved on crash | ✅ closed |
| R11 | F22–F23 (wait_for_reaction placement, level-triggered merge queue) | `wait_for_reaction` in `-stream` toolset; `merge_queue_scan()` as single source of truth | ✅ closed |
| R12 | F24 (approved_head_sha pinning), F25 (workflow toolset split + stream_binding), F26 (runstate-before-post + SENTINEL recovery) | Pinned SHA threads through merge/recovery; `workflow-orchestrator-mutating` vs `workflow-stream-mutating`; runstate written before post, SENTINEL demotes to `paused_*` on crash | ✅ closed |
| R13 | F27 (unsafe rollback on ambiguous REST failure), F28 (reaction waiter armed after post) | Post failure → demote to `paused_*` "delivery indeterminate" (not rollback-to-running); per-session `pre_post_reaction_buffer` with SENTINEL waiter arm | ✅ closed |
| R14 | F29 (reaction-key cross-session collision), F30 (SENTINEL window dropped native fast replies) | Reaction waiter key now `(channel_id, message_id, user_id)` + per-session buffer; SENTINEL-window buffer admits owner mentions OR native replies, retro-matched after swap | ✅ closed |
| **R14 result** | — | — | **No new High/Medium findings** |

## Residual (implementation-only) notes from R14

Codex flagged one non-blocking note: *add targeted tests for the two load-bearing invariants* —
- concurrent same-user reactions across two sessions (exercises per-session reaction buffer, F29),
- a native fast reply arriving in the post/swap SENTINEL window (exercises F30 retro-match).

These belong in P6 (`tests/` alongside `hermes_cli/runstate.py`) rather than the design doc.

## What's next

The design is approved for implementation. Per `plans/discord-orchestration-design.md` § Implementation phases, ship P1 → P8 as separate PRs against `nanoclaw-migration`:

1. **P1** — Scope-id plumbing.
2. **P2** — Worktree + `SandboxedToolset` (with `stream_binding` from F25).
3. **P3** — Discord admin methods + `on_raw_reaction_add`.
4. **P4** — Orchestration tools + split toolsets (`discord-orchestration-admin` / `-stream`; `workflow-orchestrator-mutating` / `workflow-stream-mutating`).
5. **P5** — Session-level routing via `deliver_message` + two-layer dispatch.
6. **P6** — Agent pool + runstate (`approved_head_sha`, `SENTINEL_PENDING`, per-session reaction buffer). **Add the two R14 invariant tests here.**
7. **P7** — Orchestrator + merge/test coordinator (`merge_queue_scan()` driven by pinned SHAs) + test-agent personas.
8. **P8** — Real-Discord bring-up per `plans/discord-orchestration-bringup-test.md`.

## Branch state

```
$ git log --oneline -2
57a822a3 docs(plans): Discord-driven multi-agent workflow orchestration design
2351210f fix(workflow,deploy): harden filename sanitization, atomicity, and deploy script
```

Working tree clean. Untracked scratch files on main (not part of this branch, leave alone):
- `plans/hermes-agent-baseline-analysis.md`
- `plans/nanoclaw-to-hermes-*.md` (3 files)
- `scripts/deploy-source.sh`
- `tests/tools/test_workflow_tools.py`
- `tools/workflow_tools.py`

## Codex-review skill invocation (kept for future regression checks)

```bash
curl -s http://127.0.0.1:4141/v1/models > /dev/null 2>&1 || \
  (copilot-api start --port 4141 > /dev/null 2>&1 & sleep 3)
curl -s http://127.0.0.1:4142/health > /dev/null 2>&1 || \
  (node ~/bin/responses-bridge/responses-bridge.mjs --port 4142 --upstream http://127.0.0.1:4141 > /dev/null 2>&1 & sleep 1)

DIFF=$(git diff main...HEAD -- plans/discord-orchestration-design.md)
echo "$DIFF" | OPENAI_API_KEY=dummy codex exec \
  -c 'model_provider="copilot"' \
  -c 'model="gpt-5.4"' \
  --skip-git-repo-check \
  "Regression review. Surface NEW High/Medium findings only. Under 250 words." 2>&1 | tail -40
```
