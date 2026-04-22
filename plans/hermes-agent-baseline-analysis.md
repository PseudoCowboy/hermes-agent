# Hermes Agent Baseline Analysis

Date: 2026-04-09
Repo: `/Users/jiangzejia/code/analysis/hermes-agent`

## Snapshot

- Hermes Agent is a full agent platform, not just a single runtime loop.
- It spans CLI, messaging gateway, OpenAI-compatible API server, ACP editor integration, cron, batch/RL tooling, skills, plugins, and multiple execution backends.
- Scale is substantial: about 753 Python files, about 339,825 Python LOC, 474 Python test files, and about 153,604 test LOC.
- Main entry surfaces are `run_agent.py`, `cli.py`, `gateway/run.py`, `acp_adapter/entry.py`, and `batch_runner.py`.

## Core Architecture

- `AIAgent` in `run_agent.py` is the runtime center.
- `run_conversation()` in `run_agent.py` owns the main synchronous conversation loop.
- The same runtime supports multiple provider/API styles including Chat Completions, OpenAI Responses, and Anthropic-style flows.
- Prompt assembly is intentionally stable across a session to preserve prompt caching and reduce costs.
- Memory writes are durable mid-session but do not mutate the active system prompt until a later rebuild.

## Tool System

- Tools are registry-driven.
- `model_tools.py` imports tool modules to trigger `registry.register()` calls.
- `tools/registry.py` is the central schema + handler registry.
- `toolsets.py` groups tool names into named toolsets for CLI, gateway, ACP, and API-server use.
- Agent-owned tools such as `todo`, `memory`, `session_search`, and `delegate_task` are intercepted by the agent loop even though they still appear in the model-facing tool surface.

## Persistence and State

- `hermes_state.py` provides SQLite + WAL + FTS5 session persistence.
- It includes schema migrations, prompt snapshots, message storage, session titles, token accounting, and full-text search.
- `gateway/session.py` adds cross-platform session identity and routing context.
- Profile-safe path handling is a first-class concern through `hermes_constants.py` and pre-import profile override logic in `hermes_cli/main.py`.

## Interface Surfaces

- CLI: `cli.py` plus `hermes_cli/commands.py` and `hermes_cli/main.py`.
- Gateway: `gateway/run.py` plus 19 platform adapter modules.
- API server: `gateway/platforms/api_server.py` exposes OpenAI-compatible endpoints.
- Editor integration: `acp_adapter/entry.py`.
- Batch/RL side: `batch_runner.py`, `environments/`, and related tooling.

## Extension Surfaces

- Plugin system: `hermes_cli/plugins.py` allows tools, hooks, and CLI commands.
- MCP: `tools/mcp_tool.py` discovers external MCP tools and registers them into the same registry surface.
- Skills: `agent/skill_commands.py`, `tools/skills_hub.py`, bundled `skills/`, and `optional-skills/`.
- Memory providers also exist as plugins under `plugins/memory/`.

## Notable Strengths

- Strong prompt-cache discipline.
- Mature context compression and handoff summaries.
- Rich provider fallback and auxiliary-model routing.
- Broad interface support across CLI, gateway, API server, and ACP.
- Real persistence and search instead of ephemeral chat-only state.
- Good extension seams via tools, plugins, skills, and MCP.

## Main Risks / Merge Hotspots

- Large orchestration files: `run_agent.py`, `cli.py`, `gateway/run.py`, and `hermes_cli/main.py`.
- Import-time discovery and global state make deep in-process composition harder.
- Broad config/env surface increases migration and integration complexity.
- Runtime behavior is modular, but orchestration responsibility is still concentrated.

## Best Future Comparison Axes

When comparing another repo against Hermes, the highest-value axes are:

- Core agent loop model and provider abstraction
- Tool schema/dispatch model
- Persistence and session model
- Config/auth/profile model
- Interface surfaces (CLI, Discord, API, editor)
- Extension model (plugins, MCP, skills)
- Multi-agent/delegation workflow model

## Validation Notes

- The repository worktree was clean at time of analysis.
- I attempted targeted pytest execution, but this checkout had no `venv/bin/activate`, so tests were not executed from this environment.
