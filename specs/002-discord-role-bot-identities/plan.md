# Implementation Plan: Discord Role Bot Identities

**Branch**: `002-discord-role-bot-identities` | **Date**: 2026-05-12 | **Spec**: [./spec.md](./spec.md)

## Summary

Add optional role-specific Discord bot identities to the existing Discord orchestration flow. The primary Discord adapter remains the only inbound router and admin actor. Auxiliary role bots are send-only clients used when an agent posts through `discord_post_message` or when the gateway auto-emits stream progress. Also close the benchmark gaps around specialist-role preservation and final project completion aggregation.

## Technical Context

**Language/Version**: Python 3.11+
**Primary Dependencies**: Existing `discord.py` integration
**Storage**: Existing gateway config and in-memory adapter state; no new persistent database
**Testing**: `pytest`, existing fake Discord adapter tests plus new unit tests for config and sender routing
**Target Platform**: Existing Hermes gateway process

## Architecture

1. Add a role-bot config model under gateway config:
   - `discord.role_bots` and `platforms.discord.role_bots` can specify role mappings.
   - Environment variable defaults support the product roles: `DISCORD_ATHENA_BOT_TOKEN`, `DISCORD_PROMETHEUS_BOT_TOKEN`, `DISCORD_ARGUS_BOT_TOKEN`, `DISCORD_APOLLO_BOT_TOKEN`, `DISCORD_ATLAS_BOT_TOKEN`, `DISCORD_HEPHAESTUS_BOT_TOKEN` plus implementation aliases like `DISCORD_FRONTEND_BOT_TOKEN`.

2. Add dispatch role metadata:
   - Extend `ToolDispatchContext` with `discord_bot_role`.
   - Orchestrator turns bind `orchestrator`.
   - Stream turns bind their manifest role (`frontend`, `backend`, later `test`).

3. Add send-only role bot clients to the Discord adapter:
   - Connect auxiliary clients after the primary client is ready.
   - Do not install message, slash-command, or reaction handlers on auxiliary clients.
   - Reuse one auxiliary client when multiple roles share a token.
   - Disconnect auxiliary clients during adapter teardown.

4. Route outbound agent-authored messages:
   - `discord_post_message` uses `adapter.send_for_role(role, ...)` when a dispatch role exists.
   - Auto-emitted stream progress uses `send_for_role` as well.
   - Missing or failed role clients fall back to `adapter.send(...)`.

5. Preserve stream execution roles:
   - `workflow_decompose` validates and writes `agentRole` into `workstreams/manifest.json`.
   - `workflow_status` exposes `agentRole` so operators can audit routing.
   - Stream bootstrap accepts `frontend`, `backend`, and `test`, then binds matching personas and Discord bot roles.

6. Aggregate project completion:
   - `update_stream_status` keeps stream transitions monotonic and updates the pinned rollup.
   - When all streams are `complete`, project runstate moves to `phase: done`.
   - A single final main-channel completion announcement is sent through the orchestrator/Athena role sender when available.

## Files

- `gateway/config.py`: role bot config parsing and env overrides
- `gateway/platforms/discord.py`: send-only role bot client manager and role-aware send helpers
- `tools/registry.py`: dispatch role field and auto-emit routing
- `tools/discord_orchestration_tools.py`: role-aware `discord_post_message`
- `gateway/session_agent_worker.py`: bind orchestrator dispatch role
- `gateway/implementer_worker.py`: bind stream dispatch role
- `gateway/project_status.py`: final all-stream completion detection and announcement
- `gateway/personas.py`, `prompts/test_agent.md`: test-agent persona registration
- `tools/workflow_tools.py`: preserve and validate `agentRole`
- `tests/gateway/test_config.py`: config/env coverage
- `tests/gateway/test_discord_role_bots.py`: role sender unit coverage
- `tests/tools/test_discord_orchestration_tools.py`: role-aware tool dispatch coverage
- `tests/gateway/test_project_status.py`: completion aggregation coverage

## Risk Notes

- A role bot cannot edit or delete a message sent by another bot. The existing pinned rollup remains owned by the primary adapter for now.
- Auxiliary clients may not have channel cache immediately. The send helper must fall back to `fetch_channel` just like the primary adapter.
- Bot tokens must not be printed in logs or serialized test outputs.
- Final completion announcements are best-effort observability. If Discord sending fails, project runstate still records stream completion and `phase: done`.

## Validation

Run focused tests:

```bash
uv run pytest tests/gateway/test_config.py tests/gateway/test_discord_role_bots.py tests/tools/test_discord_orchestration_tools.py tests/gateway/test_stream_bootstrap.py tests/gateway/test_implementer_worker.py tests/tools/test_workflow_tools.py tests/gateway/test_project_status.py
```
