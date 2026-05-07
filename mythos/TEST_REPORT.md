# Mythos Integration Test Report

**Suite:** `tests/mythos/`
**Result:** **34/34 passed** (`pytest tests/mythos/ -q`, ~1.3 s wall, parallel via xdist)
**Date:** 2026-05-07
**Mode:** all CLI agents stubbed via `FakeRunner`; Discord stubbed via `FakeDiscordTransport` (no network, no subprocesses for the orchestrator-level tests).

## What is covered

### State machine (`test_state_machine.py`, 8 tests)
Verifies the deterministic project lifecycle:
- Every phase has an entry in the transition table.
- Legal transitions: intake → planning → clarification ↔ planning → draft_ready → review → revision → planning → … → awaiting_approval → approved → decomposing → implementing → testing → completed.
- **Approval gate** (AC-003 / FR-041): implementation cannot dispatch from any phase except `approved` or `decomposing`.
- `completed` and `canceled` are terminal.
- Illegal transitions raise `IllegalTransition`.

### Config (`test_config.py`, 3 tests)
- Default agent profiles match the benchmark spec exactly:
  - Athena/Prometheus/Atlas → `claude --dangerously-skip-permissions --effort high --print` with `ANTHROPIC_BASE_URL=http://127.0.0.1:4141`, `ANTHROPIC_AUTH_TOKEN=dummy`, `ANTHROPIC_MODEL=claude-opus-4.7-1m-internal`.
  - Argus/Hephaestus → `codex exec -m gpt-5.5 -c model_reasoning_effort=high --skip-git-repo-check` with `OPENAI_BASE_URL=http://127.0.0.1:4141/v1`, `OPENAI_API_KEY=dummy`.
  - Apollo → `gemini -m gemini-3.1-pro-preview -y` with no proxy env (uses host gemini creds).
- `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `MYTHOS_MAIN_CHANNEL_ID` env vars override config.
- Per-role model overrides via `MYTHOS_<ROLE>_MODEL` rewrite the command-line `-m` flag (or append it for Claude).

### Store (`test_store.py`, 6 tests)
- Round-trip Project save/load with phase enum.
- Listing all projects.
- Reverse-lookup by Discord channel ID.
- Append-only `audit.jsonl` preserves event order.
- Multiple artifact versions coexist per project.
- **Recovery after restart (NFR-001)**: a fresh `JsonStore` reading the same `state_dir` sees prior projects, phases, tasks, and artifacts.

### Discord transport (`test_transport_routing.py`, 4 tests)
- `FakeDiscordTransport` records `send` calls per channel.
- Handler is invoked for every simulated user message.
- Sub-channels carry their parent + project ID metadata.
- Untracked channels are silently ignored by the orchestrator.

### Agent runners (`test_runners.py`, 6 tests)
- `FakeRunner` returns the registered responder's text.
- `FakeRunner` handles unregistered roles without crashing.
- `AgentPromptPacket.render_prompt()` includes role, design artifact, task brief, task ID, and the allowed-questions channel ID.
- **Real subprocess invocation verified with shell stubs:**
  - **`ClaudeCodeRunner`**: writes the prompt to stdin (matches `--print` semantics) and forwards `ANTHROPIC_BASE_URL` + `ANTHROPIC_MODEL` + `ANTHROPIC_AUTH_TOKEN` env vars.
  - **`CodexRunner`**: passes the prompt as the trailing positional argument after the configured flags.
  - **`GeminiRunner`**: passes the prompt via `-p`.

### End-to-end happy path (`test_orchestrator_happy_path.py`, 7 tests)
The full user-scenario flow runs in-process against fakes:
1. **`test_happy_path_chrome_translator_extension`** — asserts Athena ack, project channel creation, Prometheus clarification then spec, Argus review, awaiting_approval, owner approves, implementation channels created, Apollo/Atlas/Hephaestus all post in their own channels, completion announced. Verifies channel discipline (no cross-channel leakage), approval persistence, audit trail.
2. **`test_non_owner_cannot_approve`** — non-owner approval is rejected, project stays in `awaiting_approval`.
3. **`test_implementation_cannot_dispatch_without_approval`** — without approval, no specialist channels are ever created (FR-041 / AC-003).
4. **`test_two_concurrent_projects_stay_isolated`** (AC-007) — two projects from two users created simultaneously have disjoint channel IDs, separate workspaces, separate state files; approving one does not advance the other.
5. **`test_specialist_only_responds_in_own_channel`** (AC-006) — Apollo's text never appears in the project channel or backend channel.
6. **`test_state_persisted_across_restart`** (NFR-001) — fresh store reads the same project still in `awaiting_approval` with both spec and review artifacts intact.

## What was simulated vs. what was real

| Layer | Test mode |
|-------|-----------|
| Discord API | `FakeDiscordTransport` (in-memory) |
| Discord-side message receipt | `simulate_user_message()` |
| Channel + sub-channel creation | in-memory dict |
| Claude / Codex / Gemini CLI invocation (orchestrator tests) | `FakeRunner` returning canned design specs / reviews / completion summaries |
| Subprocess CLI invocation (runner tests) | **Real `subprocess.run`** against shell stubs that echo prompt + env to verify wiring |
| Filesystem workspace | real temp dirs via `pytest tmp_path` |
| JSON state store | real on-disk JSON via `JsonStore` |
| State-machine guards | real |

## Known gaps (would require live infra to test)

1. **Real Discord network**: `RealDiscordTransport` is exercised only by import (constructor lazily imports discord.py). No live test posts to a real Discord server. The `HAPPY_PATH_CHECKLIST.md` covers this.
2. **Real CLI behavior**: the runner contract is tested with shell stubs, but actual `claude` / `codex` / `gemini` exit-code conventions and output formats are only validated in operator-run smoke tests.
3. **Long-running output**: `_chunk_for_discord` chunks at 1900 chars but is not exercised by tests.
4. **Concurrent message ordering** under high load: the per-project `asyncio.Lock` is in place, but heavy concurrency tests are not yet present.

## Reproduce

```sh
cd /home/azureuser/benchmark-runs/codex-bmad/hermes-agent
pip install pyyaml pytest pytest-asyncio
python -m pytest tests/mythos/ -q
```

Expected output: `34 passed in ~1.3s`.
