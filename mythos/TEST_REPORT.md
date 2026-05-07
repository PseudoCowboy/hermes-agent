# Mythos Test Report

## Run summary

```
$ python -m pytest tests/mythos/ -v
======================== 20 passed, 4 warnings in 1.30s ========================
```

All 20 tests pass; the 4 warnings are pre-existing pytest-asyncio
"no current event loop" warnings emitted by the parent
`tests/conftest.py` (file:`tests/conftest.py:492`) and are unrelated to
mythos.

## What is simulated

The mythos test suite never invokes a real `claude`, `codex`, or
`gemini` binary. Two test doubles substitute the externally-driven
parts:

* **`FakeDiscordClient`** (`mythos/discord_ops.py`) — in-memory
  Discord. Records categories, channels, and messages. Lets a test
  inject a "user message" via `simulate_user_message()`, which fires
  the orchestrator's registered handler exactly the way discord.py
  would.
* **`FakeAgentExecutor`** (`tests/mythos/conftest.py`) — subprocess
  executor double plugged into `AgentRunner`. A test pre-pushes one
  scripted `(returncode, stdout, stderr)` per CLI invocation; the
  runner pops them in order. The recorded calls also capture the full
  `argv`, `stdin_text`, and `env`, which is how
  `test_cli_invocation_uses_required_flags` audits that the spec's
  CLI invocation contract is honored.

## Coverage matrix

| Spec acceptance criterion | Test |
|---------------------------|------|
| AC1: project channel created on intake | `test_full_happy_path` (category + plan channel, ack message) |
| AC2: Prometheus asks clarifying questions then drafts spec | `test_full_happy_path` (asserts QUESTION line + spec v1) |
| AC3: Argus posts review, Athena requests approval | `test_full_happy_path` (review + "approve" prompt in plan) |
| AC4: no implementation channels until approval | `test_full_happy_path` (frontend/backend/test absent before `approve`) |
| AC5: approval triggers decomposition + workstream channels | `test_full_happy_path` (3 channels appear after `approve`) |
| AC6 / AC7 / AC8: correct CLI per role | `test_cli_invocation_uses_required_flags` (claude/codex/gemini argv + env asserted) |
| AC9: project isolation | `test_isolation_between_concurrent_projects` (two simultaneous projects, no cross-channel leakage, distinct workspaces) |
| AC10: specialists post only in their channel | `test_full_happy_path` (Apollo/Atlas/Hephaestus appear only in their own channel) |
| FR18 / NFR8: revision loop and stale-approval invalidation | `test_revision_round` + `test_is_approved_requires_matching_version` |
| NFR1: state survives restarts | `test_state_persists_to_disk` |

## Per-file test inventory

* `tests/mythos/test_happy_path.py`
  * `test_full_happy_path` — end-to-end intake → spec → review →
    approval → decomposition → specialist runs.
  * `test_revision_round` — user requests `revise:`, second spec is
    drafted and presented for approval.
  * `test_isolation_between_concurrent_projects` — two parallel
    projects keep distinct categories, channels, and workspaces.
  * `test_state_persists_to_disk` — reloading `ProjectStore` from disk
    yields the same project record.
  * `test_cli_invocation_uses_required_flags` — Claude has
    `--dangerously-skip-permissions --effort high --print` + the three
    Anthropic env vars; Codex has
    `exec -m gpt-5.5 -c model_reasoning_effort=high --skip-git-repo-check`
    + the OpenAI env vars; Gemini has `-m … -y -p <prompt>` and uses
    no proxy env.

* `tests/mythos/test_state.py`
  * `test_slugify` — slugifies user-facing text safely.
  * `test_new_project_id_unique` — id collisions don't happen.
  * `test_is_approved_requires_matching_version` — adding a new spec
    invalidates the prior approval (the approval-gate invariant).
  * `test_channel_lookup` — channel-id → kind reverse lookup works.
  * `test_store_round_trip` — JSON-backed store survives reload.
  * `test_find_by_channel` — channel id resolves to a project after
    reload.

* `tests/mythos/test_agent_runner.py`
  * `test_build_command_claude_uses_stdin`
  * `test_build_command_codex_uses_stdin`
  * `test_build_command_gemini_appends_p_flag`
  * `test_build_env_includes_proxy_for_claude`
  * `test_build_env_includes_proxy_for_codex`
  * `test_parse_status_line_picks_last`
  * `test_parse_question`
  * `test_runner_executes_and_logs` — verifies that a successful run
    persists a structured log file under
    `<workspace>/artifacts/logs/`.
  * `test_runner_failure_returns_failed` — a non-zero exit becomes a
    `RunResult(status='failed')` and stderr ends up in the summary.

## Known gaps / things only real Discord can prove

These are deliberately not in the test suite and require the manual
checklist (`mythos/HAPPY_PATH_CHECKLIST.md`):

* That discord.py actually receives the bot token, opens a gateway
  connection, and is granted Manage Channels / Send Messages
  permissions. The unit suite stubs this out.
* That the user's installed `claude`, `codex`, and `gemini` binaries
  accept the exact argv mythos passes them. The unit suite asserts
  argv shape but does not exec the binaries.
* That the local proxy at `http://127.0.0.1:4141` (Anthropic) and
  `http://127.0.0.1:4141/v1` (OpenAI) is reachable. Real CLI
  invocations will fail loudly if the proxy is down.
* That Gemini CLI's pre-existing Google credentials are valid on the
  host running mythos.
* Long-running real agent runs may exceed the default
  `agent_timeout_seconds = 600`; tune `MYTHOS_CONFIG_PATH` for
  production workloads.

## How to reproduce

```bash
cd /home/azureuser/benchmark-runs/codex-cli-bmad/hermes-agent
python -m pytest tests/mythos/ -q
```
