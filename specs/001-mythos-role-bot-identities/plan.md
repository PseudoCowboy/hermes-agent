# Implementation Plan: Mythos Role Bot Identities

**Branch**: `codex/discord-role-bots-claude-spec-kit` | **Date**: 2026-05-12 | **Spec**: [./spec.md](./spec.md)

## Summary

Extend the `personal/bench/claude-spec-kit` Mythos implementation so each Mythos role can speak through a distinct Discord bot identity while the primary bot remains the single inbound/admin owner. Also wire final project completion aggregation when all specialist workstreams finish.

## Technical Context

**Language/Version**: Python 3.11+
**Primary Dependencies**: Existing `discord.py`, SQLite store, pytest
**Storage**: Existing `projects` SQLite table with one additive JSON column for completed disciplines
**Runtime**: `python -m mythos.run`

## Approach

1. Add role token config:
   - Environment variables: `DISCORD_<ROLE>_BOT_TOKEN` and `MYTHOS_<ROLE>_BOT_TOKEN`.
   - YAML: `discord_role_bots: {prometheus: ..., ...}`.

2. Extend Discord adapters:
   - `DiscordAdapter.start(..., role_tokens=...)` accepts optional send-only role tokens.
   - `DiscordPyAdapter` starts auxiliary clients without message handlers and reuses a client when roles share a token.
   - `FakeDiscordAdapter` records `author_role` for tests.

3. Route agent messages:
   - Add `MythosOrchestrator._send_agent(role, channel_id, content)`.
   - Use it for Athena, Prometheus, Argus, Apollo, Atlas, and Hephaestus messages.

4. Aggregate completion:
   - Add `completed_disciplines` to `Project` and SQLite persistence.
   - Mark a discipline complete when its output includes `<DISCIPLINE> WORK COMPLETE`.
   - Move the project to `complete` and post one Athena summary once all discipline channels are complete.

5. Bake in benchmark runner compatibility:
   - Add the Codex sandbox bypass flag noted in `results/summary.md` to the default Codex command.
   - Assert the flag in config tests so live Mythos runs do not depend on an out-of-band script patch.

## Validation

```bash
uv run pytest tests/mythos -q
uv run python -m compileall mythos tests/mythos
```
