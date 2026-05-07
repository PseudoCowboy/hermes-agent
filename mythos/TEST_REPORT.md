# Mythos — Integration Test Report

**Run date:** 2026-05-07
**Branch:** `bench/codex-spec-kit`
**Command:** `python -m pytest tests/mythos/ -q`
**Result:** **26 passed, 0 failed** in 1.40 s.

The Mythos test suite uses an in-memory `FakeDiscordTransport` plus a
`FakeAgentRunner` that returns canned (or computed) responses. The exact
same `Orchestrator` code path the production wiring uses is exercised —
only the I/O at the edges is faked.

---

## Files

| Test file                                | Coverage |
|------------------------------------------|----------|
| `tests/mythos/test_agent_roster.py`      | Fixed mapping `(role → mythological name → CLI backend)`. Regression guard against silently changing Athena's CLI, etc. |
| `tests/mythos/test_state_machine.py`     | `Project` FSM: legal/illegal transitions, can't skip approval, revision loop, spec versioning. |
| `tests/mythos/test_config.py`            | Default model + endpoints + env vars match the build instructions exactly for Claude Code, Codex, and Gemini. CLI argv shape verified. YAML overrides apply. |
| `tests/mythos/test_orchestrator_e2e.py`  | End-to-end happy path, clarifying-question loop, blocking-review revision round, two concurrent projects (isolation), specialist channel confinement, casual-message rejection, agent-failure → BLOCKED. |

---

## What is simulated

Each `e2e` test injects a sequence of "user posts in channel X" events
into the orchestrator and asserts on the resulting state changes and
on the messages that were sent back through the transport.

* **`test_happy_path_chrome_translator_extension`** — full lifecycle
  for the user-scenario.md example. Asserts: project channel created,
  spec drafted, review posted, user-approval prompt, decomposition into
  3 workstream channels, all 3 specialists run with the spec in their
  prompt, project ends in `COMPLETE`.
* **`test_clarifying_question_round`** — first draft returns
  `CLARIFY:`, system asks user, user answers, second draft produces
  spec.
* **`test_review_blocking_triggers_revision_round`** — first review
  ends `STATUS: REVISE`, system loops back to draft plan, second
  review approves.
* **`test_isolation_two_concurrent_projects`** — two requests posted
  in parallel get separate channels, separate workdirs, separate
  workstream channel names; no slug from project A appears in project
  B's channel names. Registry routes messages by channel correctly.
* **`test_specialist_only_responds_in_assigned_channel`** — a user
  message in a workstream channel produces a reply in *that* channel
  only; the project channel and sibling workstream channels gain no
  new messages (FR-018, FR-022).
* **`test_main_channel_non_request_is_ignored`** — Athena's intake
  classifier returns `IGNORE`, no project is created, the bot still
  acknowledges politely.
* **`test_agent_failure_marks_project_blocked`** — Prometheus raises
  `AgentExecError`, project transitions to `BLOCKED`, a recoverable
  status message is posted in the project channel.

---

## Spec → test mapping

| Spec requirement                | Test |
|---------------------------------|------|
| FR-001/FR-002 main channel intake & per-project channel | `test_happy_path_*` |
| FR-003/SC-002 project isolation | `test_isolation_two_concurrent_projects` |
| FR-004…FR-009 fixed CLI backends | `test_agent_roster.*` + `test_config.test_subprocess_command_shapes` |
| FR-010 clarifying questions     | `test_clarifying_question_round` |
| FR-011 spec/review versioning   | `test_state_machine.test_specs_versioned`, asserted via `project.specs` in e2e |
| FR-012/FR-013 explicit user approval | `test_state_machine.test_cannot_decompose_before_approval` + e2e `approve` step |
| FR-014 ≥2 revision rounds       | `test_review_blocking_triggers_revision_round` (1 round triggered, second approved within `max_revision_rounds=2`) |
| FR-015/FR-016 decomposition + dedicated channels | `test_happy_path_*` |
| FR-017 specialists receive spec | e2e prompt-content assertion |
| FR-018/FR-022 channel confinement | `test_specialist_only_responds_in_assigned_channel`, isolation test |
| FR-021 failure reporting        | `test_agent_failure_marks_project_blocked` |
| Edge: casual main-channel chatter | `test_main_channel_non_request_is_ignored` |

---

## What is **not** simulated (manual test required)

* **Real Discord round-trip.** `HermesDiscordTransport` (the production
  implementation in `mythos/transport.py`) is not exercised — testing it
  needs a live bot token and guild. The user must walk through
  `mythos/HAPPY_PATH_CHECKLIST.md` to verify it.
* **Real `claude` / `codex` / `gemini` subprocesses.** `SubprocessAgentRunner`
  is only validated for argv/env shape. Whether the host has the right
  CLI versions installed and authenticated is the operator's setup
  problem (covered in `SETUP.md`).
* **Persistence across restarts.** Project state is in-memory only;
  restarting `python -m mythos.app` orphans in-flight projects. This is
  noted in `HAPPY_PATH_CHECKLIST.md` and is a documented gap.
* **Discord rate-limit handling.** Mythos relies on discord.py's built-in
  rate-limit handling; nothing extra is layered on top.

---

## How to reproduce

```bash
cd /path/to/hermes-agent
python -m pytest tests/mythos/ -q
```

Expected output:

```
26 passed, 4 warnings in ~1–2s
```

The 4 warnings come from the repo-wide `tests/conftest.py` (deprecated
`asyncio.get_event_loop_policy().get_event_loop()` usage) and are not
Mythos-specific.
