# Mythos — integration test report

**Test command**: `pytest tests/mythos/ -q`
**Last run**: all green, 10/10 passing in ~2s.

## What is being simulated

The integration tests use two seams to avoid real external dependencies:

1. **`FakeDiscordAdapter`** (`mythos/discord_adapter.py`) — an in-memory
   stand-in for Discord. It records every outbound `send_message`,
   `create_text_channel`, and `create_category`, and exposes
   `simulate_user_message(channel_id, user_id, content)` so tests can inject
   inbound messages without any network I/O.

2. **`ScriptedRunner`** (`tests/mythos/scripted_runner.py`) — replaces
   `mythos.cli_runner.run_cli`. Returns canned outputs based on the agent's
   `cli.kind` (`claude` / `codex` / `gemini`) and small content matches on the
   prompt, e.g. Prometheus returns the canned translator spec when given the
   chrome translator seed; Argus always returns a structured review ending in
   `RECOMMENDATION: APPROVE`; specialists return their `XYZ WORK COMPLETE`
   sentinel.

This means the orchestrator runs the **real** routing, locking, state machine,
SQLite persistence, channel creation, and per-channel confinement logic; only
the network and subprocess tips of the iceberg are mocked.

## Test cases

| File | Test | Verifies |
|---|---|---|
| `test_orchestrator.py` | `test_main_channel_post_creates_isolated_project_channel` | US1: main-channel post → ack + new channel + seed message + `@Prometheus` ping; project row persisted. |
| `test_orchestrator.py` | `test_full_happy_path_drafts_reviews_approves_decomposes` | US1→US6 end-to-end: draft v1 → Argus review → Athena approval prompt → owner `approve` → discipline channels created → all three specialists post `WORK COMPLETE` in their own channels and **not** in others; approval row persisted. |
| `test_orchestrator.py` | `test_two_concurrent_projects_stay_isolated` | US7: two overlapping projects keep separate channels and state; approving project 1 does not start work on project 2. |
| `test_orchestrator.py` | `test_revise_loop_is_bounded` | FR-007: `revise` more than `max_review_iterations` times triggers a max-rounds-reached message instead of looping. |
| `test_orchestrator.py` | `test_only_owner_can_approve` | FR-014/edge case: only the project owner's approval moves the workflow forward. |
| `test_orchestrator.py` | `test_specialist_confinement_only_responds_in_own_channel` | FR-010: a specialist mention in the *parent* project channel does not cause the specialist to post in its sub-channel. |
| `test_store_and_config.py` | `test_store_roundtrip` | SQLite persistence for projects, spec versions, reviews, approvals. |
| `test_store_and_config.py` | `test_get_project_by_channel` | Channel-id → project lookup used by the router. |
| `test_store_and_config.py` | `test_config_defaults_and_env_overrides` | Per-agent CLI defaults match the spec; env vars override base URL / model. |
| `test_store_and_config.py` | `test_yaml_overrides` | YAML config file overrides timeouts and per-agent env. |

## Latest run output (verbatim, abbreviated)

```
======================== 10 passed, 1 warning in 2.05s =========================
```

(The single warning is from hermes-agent's repo-wide `tests/conftest.py` calling
`asyncio.get_event_loop_policy().get_event_loop()`; not from mythos code.)

## Known gaps (intentional, for v1)

These are deferred — they are not blockers for the spec's happy path but a
production deployment should address them:

1. **No real-Discord smoke test in CI.** The `DiscordPyAdapter` itself is not
   exercised — only the `FakeDiscordAdapter`. A `discord.py`-typed unit test
   could mock the discord client object, but this run defers to manual
   testing via `mythos/HAPPY_PATH_CHECKLIST.md`.
2. **No retry / backoff on Discord rate limits.** discord.py's library default
   handles 429s, but mythos itself does not surface them as channel messages
   yet (FR-016 partially covered: CLI failures are surfaced; Discord failures
   only on channel-creation).
3. **Decomposition heuristic is keyword-based.** `_detect_disciplines` looks
   for "frontend" / "backend" / "test" tokens. If Prometheus produces a spec
   that uses other vocabulary, mythos may pick the wrong subset. A future
   iteration should let Prometheus explicitly enumerate disciplines in a
   structured tail block.
4. **Specialist agents do not loop.** Each specialist runs once per
   user-message in its sub-channel; there is no autonomous follow-up
   iteration (e.g., "check tests pass, then mark complete"). The completion
   sentinel is detected but not yet wired to advance the project to
   `COMPLETE` state.
5. **No per-CLI auth probing.** If `claude` / `codex` / `gemini` is missing
   or unauthenticated, the user only learns when the first call fails inside
   a project channel. A startup probe would be cheap to add.
6. **The hermes-agent gateway is not used.** Mythos owns its own discord.py
   client (re-using the same `discord.py` dependency hermes already pulls in)
   because hermes' adapter binds each chat to a hermes session, which
   conflicts with our per-agent confinement model. Documented in
   `mythos/discord_adapter.py`.

## How to reproduce

```bash
cd /home/azureuser/benchmark-runs/claude-spec-kit/hermes-agent
pytest tests/mythos/ -q
```
