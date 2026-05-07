# Mythos Integration Test Report

**Branch:** `bench/claude-bmad`
**Date:** 2026-05-07
**Run:** `pytest tests/mythos/ -q`
**Result:** ✅ **34 / 34 passed in ~1.3 s**

## Test inventory

| File                                | Cases | What it covers |
|-------------------------------------|-------|----------------|
| `tests/mythos/test_happy_path.py`   |  1    | Full intake → spec → review → approval → decomposition → specialist completion. Asserts every channel was created, every agent posted, every specialist confined to its channel, and final `phase == COMPLETE`. |
| `tests/mythos/test_isolation.py`    |  3    | Two concurrent projects don't share channels, slugs, or workspaces. Messages in unrelated channels are ignored. Mid-stream user follow-ups bump the approval round. |
| `tests/mythos/test_mediator.py`     |  9    | Approve / reject parsing, freeform-as-change-request, escalation after `max_approval_rounds`, malformed review verdict defaults to `changes_requested`, multi-round live flow. |
| `tests/mythos/test_roles.py`        |  7    | Default Claude/Codex/Gemini commands match the spec verbatim, env defaults are correct, env var overrides apply to URL/model/key for all three backends. |
| `tests/mythos/test_cli_agent.py`    |  5    | `CliAgent` pipes prompt to stdin (Claude/Codex), appends prompt as last arg (Gemini), runs in the supplied workdir, surfaces non-zero exit codes + stderr, raises `AgentError` on missing binary. Uses a tiny shell-script fake CLI so no real CLI install is required. |
| `tests/mythos/test_slug_and_workspace.py` | 9 | Slug derivation, stopword filtering, deterministic seeded slugs, workspace dir layout, role → workdir routing, spec versioning, log append. |

## What was simulated

- **Discord I/O:** `mythos/discord_io.InMemoryDiscordIO` records every
  `send` / `create_category` / `create_text_channel` call and lets tests
  inject inbound messages via `inject_user_message(channel_id, content)`.
  No `discord.py` import is needed by the test path.
- **Agent CLIs:** `mythos/agents.StubAgent` returns a canned response per
  role. The full Athena/Prometheus/Argus/Atlas/Apollo/Hephaestus pipeline
  runs end-to-end in <1.5 s with no `claude` / `codex` / `gemini` binary
  on the host. A separate `test_cli_agent.py` covers `CliAgent`'s real
  subprocess path against a 5-line shell script that emulates each CLI's
  contract.

## What real-world coverage still requires

- **Real Discord:** the `RealDiscordIO` adapter is wired the same way as
  hermes-agent's `gateway/platforms/discord.py` (same `discord.py`
  library, same intents) but is not exercised in CI. The
  `mythos/HAPPY_PATH_CHECKLIST.md` runbook is the explicit human-driven
  smoke test for the Discord side.
- **Real CLIs:** invocation contracts (env vars, command flags) are
  asserted exactly in `test_roles.py`. Whether the configured local
  proxy at `127.0.0.1:4141` honors those env vars is environment-specific
  and not tested in the suite.
- **Crash recovery from `state.json`:** the state file is written every
  phase change, but the recovery code-path that re-hydrates a project on
  startup is not implemented in v1 (Discord remains the source of truth;
  the `state.json` is a hint). Documented in `SETUP.md` §7.

## Known gaps vs the architecture spec

These deferments are conscious; each maps to "Out of Architecture" §9 in
the spec or to "Nice to Have" in the PRD §Project Scoping.

1. **No slash commands** (`/status`, `/archive`, `/health`). The PRD
   marks these *Nice to Have*. Adding them is straightforward — register
   discord.py app commands inside `RealDiscordIO.start()`.
2. **No CLI version pinning verification at startup** (NFR16). The
   default invocations specify a model name but not a CLI binary version.
3. **No automatic re-hydration** of in-progress projects after a process
   restart. The `state.json` persistence is wired, but the loader path is
   not (Discord history is the system of record per NFR6).
4. **Question detection** uses a `QUESTION:` line convention rather than
   parsing the agent's full output for question semantics. This is
   deliberate (per the spec's "envelope-in-message" pattern §4.4) — easy
   for the agent to obey and easy for the orchestrator to detect.

## Pre-existing hermes-agent test failures

`pytest tests/tools/test_delegate.py` reports 4 failures on `main`
unrelated to Mythos (credential-resolution edge cases and a heartbeat
timing test). Confirmed reproducible without any Mythos changes by
running on the unmodified branch. Mythos does not touch
`tools/delegate_tool.py` or `gateway/platforms/discord.py`.

## How to reproduce

```bash
cd hermes-agent
pip install -e ".[messaging,dev]"
pytest tests/mythos/ -q
```

Expected: `34 passed in <2s`.
