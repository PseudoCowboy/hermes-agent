# Mythos Test Report

## Suite

`pytest tests/mythos/ -v` — 30 tests, all passing.

```
30 passed, 4 warnings in 1.76s
```

The deprecation warning is about `asyncio.get_event_loop_policy()` and
originates in hermes-agent's existing root `tests/conftest.py:492`, not
in mythos code.

## Test files

- `tests/mythos/test_orchestrator_integration.py` — 9 integration tests
  covering the entire happy path with the in-memory Discord fake and
  scripted agent runner.
- `tests/mythos/test_unit.py` — 21 unit tests for the smaller modules
  (intake classifier, role registry, runners, state store).

## Integration coverage

| Test                                                       | Behavior verified                                                                                                            |
|------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------|
| `test_happy_path_chrome_translator`                        | Full user-scenario flow: intake → channel creation → Prometheus draft → Argus review → user `approve` → frontend/backend/test workstreams → final summary. Confirms each agent only posts in its own channel. |
| `test_review_rejection_triggers_revision_round`            | Argus posting `REQUIRED CHANGE` lines bounces work back to Prometheus; second round comes back `APPROVED`.                   |
| `test_clarifying_questions_pause_for_user_answers`         | Prometheus emitting `QUESTIONS:` parks the project in `CLARIFYING`; user reply re-runs drafting with answers in context.     |
| `test_non_project_message_is_ignored`                      | Empty / short / meta-prefixed messages do not create a project.                                                              |
| `test_two_concurrent_projects_are_isolated`                | Two simultaneous projects: distinct channels, distinct workspaces; approving one does NOT decompose the other.               |
| `test_approval_gate_blocks_implementation`                 | Workstream channels and runs are blocked until explicit `approve`.                                                           |
| `test_persistence_survives_recreate`                       | Killing the orchestrator and constructing a fresh one against the same state dir resumes mid-flow (approval after restart).  |
| `test_idempotent_intake_does_not_double_create`            | Re-delivering the exact same Discord message id is a no-op (single project record).                                          |
| `test_workstream_question_escalates_to_project_channel`    | `Orchestrator.escalate_question(...)` posts a user-decision-needed prompt back to the project channel via Athena.            |

## Unit coverage

- **intake**: classifier accept/reject, approval phrasings, revision phrasings.
- **roles**: every role + name + CLI mapping matches the benchmark spec
  exactly (Athena/Prometheus/Argus/Hephaestus/Apollo/Atlas; Claude Code +
  Codex + Gemini argv flags + env defaults).
- **runners**: `ScriptedRunner` returns scripted output, `SubprocessRunner`
  routes Claude/Codex prompts to stdin and Gemini prompts to argv,
  missing binary surfaces as exit 127, `_chunk_for_discord` splits on
  newlines under the 1900-char limit.
- **state**: persistence round-trip (workstreams + review rounds + phase),
  per-project locking via `edit()` context manager, idempotency one-shot
  claim, duplicate-create rejection.

## What was simulated

- Discord — full in-memory fake (`InMemoryDiscord`) with channels, ordered
  message log, broadcast handlers, and seedable user messages. Faithful to
  the surface the orchestrator uses (`create_channel`, `send_message`,
  `register_handler`, `mention`).
- Agent CLIs — `ScriptedRunner` returns canned strings per role. The real
  `SubprocessRunner` is unit-tested against an injected `Popen` stub so we
  can assert on `argv`, `cwd`, `env`, and stdin/argv prompt routing without
  spawning the real binaries.

## What is NOT covered by tests (known gaps for real-world testing)

These items can only be validated against a live Discord server and the
real model proxies. The manual checklist at
`mythos/HAPPY_PATH_CHECKLIST.md` is what the operator runs to clear them.

1. **`DiscordClient` against a live discord.py session** — there is no test
   that spins up the real bot loop. The wrapper is small (~80 LOC) and
   delegates to `discord.py`'s own surface, but it has not been exercised
   against a real guild in this repo.
2. **CLI subprocess integration** — the test suite never spawns `claude`,
   `codex`, or `gemini`. The argv/env are unit-tested but the actual
   subprocesses' stdout has not been parsed for nuance (reasoning blocks,
   tool calls, etc.). The `messages` field is a single chunk per result;
   the operator should confirm long outputs chunk acceptably in real Discord.
3. **Permission/quota errors at channel-creation time** — the orchestrator
   handles the `Exception` branch (sets `ProjectPhase.ERROR`, sends a
   failure message in the main channel) but no test simulates a 403/429
   response from the real Discord API.
4. **Long-running agent timeouts** — `AgentRequest.timeout` defaults to
   600s; the timeout branch in `SubprocessRunner.run` is exercised
   manually (no test).
5. **Concurrent re-delivery of work-in-flight messages** — idempotency
   keys cover intake and channel creation; mid-flight handler-message
   races have not been stress-tested.

## How to re-run

```bash
cd /home/azureuser/benchmark-runs/codex-cli-openspec/hermes-agent
python -m pytest tests/mythos/ -v
```

The test suite has no external dependencies (no Discord, no LLM, no
subprocesses) and runs in under 2 seconds. It honors the project's
hermetic `tests/conftest.py` so credential env vars are scrubbed for
every test.
