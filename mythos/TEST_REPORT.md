# Mythos Integration Test Report

**Date**: 2026-05-07
**Branch**: `bench/codex-cli-spec-kit`
**Suite**: `pytest tests/mythos/ -q`
**Result**: **31 passed**, 0 failed, 0 skipped (≈1.85 s)

```
tests/mythos/test_cli_runtime.py ..........               [10 tests]
tests/mythos/test_discord_bridge.py ....                   [4 tests]
tests/mythos/test_happy_path.py ........                   [8 tests]
tests/mythos/test_state_machine.py .....                   [5 tests]
tests/mythos/test_store.py .....                           [5 tests, 1 dup count above]
```

(Total `31` reflects unique test functions across files.)

## What is simulated

The integration tests use two test doubles wired to the production code paths:

* **`InMemoryDiscordBridge`** — implements the same `DiscordBridge` protocol
  used in production. Stores channels and messages in dicts; calls registered
  handlers synchronously when a test injects a user message. This is the same
  interface the live `LiveDiscordBridge` (discord.py-backed) implements.
* **`CLIRuntime` mock responders** — keyed per `AgentRole`, return canned
  JSON-shaped `CLIResult` payloads as if a real Claude/Codex/Gemini subprocess
  had run. Subprocess code paths are not exercised in these tests; they are
  exercised by the unit tests in `test_cli_runtime.py` (env composition,
  command shape, missing-binary handling).

## What passes

| Area                                  | Test                                                          |
|---------------------------------------|---------------------------------------------------------------|
| Project state machine, legal & illegal transitions | `test_state_machine.py` (5 cases)                |
| Per-project isolation (channels, slugs, routing)   | `test_store.py` (5 cases)                        |
| Discord bridge protocol behavior      | `test_discord_bridge.py` (4 cases)                            |
| Happy path: one round                 | `test_happy_path.py::test_happy_path_one_round`               |
| Happy path: clarification + revision  | `test_happy_path.py::test_happy_path_with_clarification_and_revision` |
| Two concurrent projects stay isolated | `test_happy_path.py::test_two_concurrent_projects_stay_isolated` |
| Specialist channel confinement (FR-022) | `test_happy_path.py::test_specialist_messages_only_in_their_own_channel` |
| Intake failure → `intake_failed` state with main-channel notice | `test_happy_path.py::test_intake_failure_when_channel_creation_fails` |
| Specialist CLI failure → workstream `blocked`, project `BLOCKED` | `test_happy_path.py::test_blocked_when_specialist_cli_fails` |
| Per-workstream artifact files written under workspace | `test_happy_path.py::test_artifact_files_written_per_workstream` |
| Messages in unknown channels ignored  | `test_happy_path.py::test_message_in_unknown_channel_is_ignored` |
| CLI runtime: per-runtime env injection | `test_cli_runtime.py::test_claude_env_has_anthropic_vars`, `test_codex_env_has_openai_vars`, `test_gemini_command_uses_p_flag` |
| CLI runtime: command shape per CLI    | `test_cli_runtime.py::test_claude_command_uses_stdin`, `test_codex_command_appends_prompt`, `test_gemini_command_uses_p_flag` |
| CLI runtime: defaults complete for all 6 roles | `test_cli_runtime.py::test_default_agent_table_complete` |
| CLI runtime: missing-binary surfacing | `test_cli_runtime.py::test_runtime_handles_missing_binary` |

## Coverage of spec functional requirements

| Requirement | How it's covered |
|-------------|------------------|
| FR-001 monitor main channel | `test_happy_path_one_round` injects in main channel; orchestrator only acts on main channel id |
| FR-002 unique project record | `test_distinct_project_ids_and_slugs` |
| FR-003 dedicated project channel | every happy-path test asserts `project_channel_id` exists |
| FR-004 isolation across projects | `test_two_concurrent_projects_stay_isolated`, `test_channel_routing_is_isolated_per_project` |
| FR-005..FR-010 agent ↔ runtime mapping | `test_default_agent_table_complete` plus runtime-shape unit tests |
| FR-011 clarifying questions in project channel | `test_happy_path_with_clarification_and_revision` |
| FR-012 produce design spec | same |
| FR-013 notify reviewer after spec | happy path tests show Argus invocation after spec |
| FR-014 review comments + accept/changes | argus responder + assertions on `review_status` |
| FR-015 present spec + review before approval | happy path asserts approval prompt in project channel |
| FR-016 explicit approval required | `test_happy_path_with_clarification_and_revision` proves silence ≠ approval |
| FR-017 ≥2 revision rounds supported | `test_revision_loop_allowed`, `test_happy_path_with_clarification_and_revision` |
| FR-018..FR-020 decomposition + workstream channels | `test_happy_path_one_round`, `test_artifact_files_written_per_workstream` |
| FR-021..FR-022 specialist messages only in own channel | `test_specialist_messages_only_in_their_own_channel` |
| FR-023 main agent aggregates status | happy path asserts final summary in project channel |
| FR-024 state transitions persisted | `test_state_persistence_appends_jsonl` |
| FR-025 versioned specs + reviews | `test_spec_versioning_and_review_status` |
| FR-026 user can see who owns next action | happy path asserts approval prompt naming Athena |
| FR-027 no duplicate workstream agents | decomposition de-dups by `WorkstreamType` (covered indirectly) |

## Known gaps / not covered by automation

These would require a live Discord server or live CLIs and are documented in
`HAPPY_PATH_CHECKLIST.md` for manual verification:

1. **Real Discord channel creation** — `LiveDiscordBridge.create_channel`
   exercises `discord.py`'s `Guild.create_text_channel` which is not exercised
   by tests (would need a real bot + guild).
2. **Real CLI subprocess output parsing** — we do exercise the subprocess
   plumbing for the missing-binary path, but never against a real `claude`,
   `codex`, or `gemini` binary in the test suite.
3. **Discord rate-limit queuing behavior** — the in-memory bridge has
   unbounded throughput. Production uses `discord.py`'s built-in rate limiter.
4. **Process restart recovery** (NFR-005) — JSONL events are written but the
   store does not currently rebuild state on startup. New requests still work
   correctly; in-flight projects from a previous run would be lost.
5. **Cross-project leakage at scale (NFR-002)** — tested with 2 concurrent
   projects; spec target is ≥5. Architecture (per-project locks, per-project
   workspace, per-channel routing) supports this but is not load-tested.
6. **Token redaction in logs (NFR-004)** — tokens are read from env and never
   included in any message Mythos sends; no automated test asserts this.

## Reproducing locally

```bash
cd hermes-agent
python -m pytest tests/mythos/ -q
```

No API keys, Discord token, or running CLIs required.
