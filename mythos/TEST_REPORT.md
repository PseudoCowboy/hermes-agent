# Mythos Test Report

## Suite

`pytest tests/mythos/ -q` — 22 tests, all passing in ~2 seconds.

```
tests/mythos/test_happy_path.py
  test_happy_path_translator_extension                ✓
  test_provider_routing_is_fixed                      ✓

tests/mythos/test_isolation.py
  test_two_concurrent_projects_get_distinct_channels  ✓
  test_intake_is_idempotent                           ✓
  test_cross_project_routing_is_blocked               ✓
  test_state_is_persisted_per_project                 ✓
  test_implementation_does_not_run_without_approval   ✓
  test_user_rejection_loops_back_to_planning          ✓

tests/mythos/test_approval_workflow.py
  test_review_revision_loop                           ✓
  test_approval_records_user_design_and_decision      ✓
  test_unknown_text_in_project_channel_does_not_approve ✓

tests/mythos/test_units.py (10 unit tests)
  slugify, chunk_message, protocol parsing,
  role->provider map, config defaults & env overrides,
  workspace allocation                                ✓✓✓✓✓✓✓✓✓✓
```

## What is simulated

The integration tests use two purpose-built fakes:

- **`FakeDiscordClient`** (`mythos/testing/fake_discord.py`) — implements the
  `DiscordClient` Protocol with in-memory channels and message logs. Tests
  drive it with `await fake_discord.user_says(...)` and assert against
  `fake_discord.messages_in(channel_id)`.
- **`ScriptedRunner`** (`mythos/runners/scripted.py`) — implements the
  `Runner` Protocol; tests pre-queue per-role replies (one entry per call).
  No subprocesses are ever spawned during testing.

Together these let us exercise the full state machine — intake → planning →
review → revision loop → approval gate → decomposition → frontend/backend
parallel execution → test phase — without Discord credentials or CLI
binaries.

## What is covered

- ✅ User-scenario.md happy path end-to-end (`test_happy_path_translator_extension`).
- ✅ Approval gate prevents implementation channels from being created until
  the user types `approve`.
- ✅ Revision loop: `request_changes` review feeds back to Prometheus and
  triggers a fresh review.
- ✅ Approval records user ID, design version, and decision.
- ✅ Two concurrent projects get distinct project IDs, channels, and
  workspaces.
- ✅ Intake is idempotent by source message ID.
- ✅ Cross-project routing raises `CrossProjectError` and is recorded as
  `cross_project_blocked` in the audit log.
- ✅ Per-project JSON state persistence under `state_dir`.
- ✅ User rejection loops back to PLANNING and records `decision="rejected"`.
- ✅ Random user text in the project channel does **not** count as approval.
- ✅ Role → provider mapping is fixed: Athena/Prometheus/Atlas → Claude,
  Argus/Hephaestus → Codex, Apollo → Gemini.
- ✅ Config loader default values match the build spec exactly (commands,
  endpoints, model names, env vars).
- ✅ Config env-var overrides work for base URL and model names.
- ✅ Workspace allocator creates `frontend/`, `backend/`, `test/`, `docs/`,
  `shared/` subdirs per project.

## What is NOT covered (manual / live-only)

- Real Discord gateway behaviour: rate limiting, intent denials, channel
  creation under restrictive permissions, DMs vs guild channels. Use the
  `HAPPY_PATH_CHECKLIST.md` to validate this against a real server.
- Real CLI behaviour: whether `claude`, `codex`, `gemini` actually emit the
  `<<MYTHOS:STATUS:*>>` markers when prompted with `prompts.py` text. The
  prompts instruct the agents to do so; if a particular model refuses, the
  orchestrator falls back to "treat the whole reply as a single finished
  message" (see `parse_agent_output` default).
- Multi-turn dialogue with the implementation agents. Today they are
  one-shot per task — questions are surfaced in the channel but the
  orchestrator does not currently re-invoke the agent with the user's
  reply. This is an obvious next-iteration extension of `_handle_project_channel`.
- The discord.py adapter (`DiscordPyClient`) is not unit-tested because it
  is a thin shim; it imports `discord` (in `[messaging]` extra) at
  construction time. Behaviour is verified manually via
  `HAPPY_PATH_CHECKLIST.md`.

## Known gaps

1. **Implementation-agent Q&A loop** — see above. If Apollo asks a question
   in the frontend channel today, the orchestrator records the message but
   does not re-invoke Apollo with the user's answer. Easy follow-up.
2. **No retry policy on CLI failures** — `SubprocessRunner` returns the
   error in `RunResult.error` and the orchestrator surfaces it as a
   `STATUS:failed` post in the channel. There is no automatic retry; an
   operator must intervene.
3. **No timeout for the user's approval** — if the user never types
   `approve`, the project sits in `AWAITING_APPROVAL` forever. Acceptable
   for now (matches spec); a TTL would be a future enhancement.
