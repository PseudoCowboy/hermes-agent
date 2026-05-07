"""Mythos configuration loading.

A single ``MythosConfig`` describes:

* Discord wiring (guild id, main channel id).
* Per-role agent identity (display name, avatar URL).
* Per-role runner binding (which CLI runner backs the role) and the
  command/env defaults that runner should use.
* Workflow tunables (max planning rounds, handoff streak, per-call timeout).

Configuration sources, highest priority first:

1. Explicit ``MythosConfig`` constructor arguments.
2. ``config.yaml`` ``mythos:`` section.
3. Environment variables (``MYTHOS_*``, plus the standard ``DISCORD_*`` /
   ``ANTHROPIC_*`` / ``OPENAI_*`` / ``GEMINI_*`` ones).
4. Hard-coded defaults that match the design doc's role-to-CLI table.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - yaml ships with most installs
    yaml = None


# ---------------------------------------------------------------------------
# Defaults straight from the design doc / benchmark task brief.
# ---------------------------------------------------------------------------

DEFAULT_AGENT_ROLES = ("athena", "prometheus", "argus", "hephaestus", "apollo", "atlas")

DEFAULT_AGENT_IDENTITIES: Dict[str, Dict[str, str]] = {
    "athena":     {"display_name": "Athena",     "role": "main",       "channel": "main"},
    "prometheus": {"display_name": "Prometheus", "role": "draft_plan", "channel": "planning"},
    "argus":      {"display_name": "Argus",      "role": "review",     "channel": "planning"},
    "hephaestus": {"display_name": "Hephaestus", "role": "test",       "channel": "test"},
    "apollo":     {"display_name": "Apollo",     "role": "frontend",   "channel": "frontend"},
    "atlas":      {"display_name": "Atlas",      "role": "backend",    "channel": "backend"},
}

DEFAULT_RUNNER_BINDING: Dict[str, str] = {
    "athena":     "claude_code",
    "prometheus": "claude_code",
    "argus":      "codex",
    "hephaestus": "codex",
    "apollo":     "gemini",
    "atlas":      "claude_code",
}

# Subprocess defaults requested by the benchmark brief.
DEFAULT_RUNNER_COMMANDS: Dict[str, Dict[str, Any]] = {
    "claude_code": {
        "command": ["claude", "--dangerously-skip-permissions", "--effort", "high", "--print"],
        "env": {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4141",
            "ANTHROPIC_AUTH_TOKEN": "dummy",
            "ANTHROPIC_MODEL": "claude-opus-4.7-1m-internal",
        },
    },
    "codex": {
        "command": ["codex", "exec", "-m", "gpt-5.5", "-c", "model_reasoning_effort=high", "--skip-git-repo-check"],
        "env": {
            "OPENAI_BASE_URL": "http://127.0.0.1:4141/v1",
            "OPENAI_API_KEY": "dummy",
        },
    },
    "gemini": {
        # Uses Gemini CLI's preconfigured credentials on the host - no proxy.
        "command": ["gemini", "-m", "gemini-3.1-pro-preview", "-y"],
        "env": {},
    },
}

DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_PLANNING_ROUNDS = 2
DEFAULT_MAX_HANDOFF_STREAK = 6
DEFAULT_WORKSPACE_ROOT = "./workspaces"


@dataclass
class AgentSpec:
    """Per-agent configuration."""

    name: str  # e.g. "athena"
    display_name: str
    role: str  # logical role: main, draft_plan, review, frontend, backend, test
    channel: str  # which sub-channel this agent is bound to ("main", "planning", "frontend", ...)
    runner: str  # which runner key in MythosConfig.runners to use
    avatar_url: Optional[str] = None


@dataclass
class RunnerSpec:
    """Subprocess-style runner definition."""

    name: str  # "claude_code" | "codex" | "gemini"
    command: List[str]
    env: Dict[str, str] = field(default_factory=dict)
    # When True, runner appends ``-p <prompt>`` style argument; otherwise the
    # prompt goes via stdin. ``gemini -p`` uses argv, ``claude --print`` and
    # ``codex exec`` accept stdin.
    prompt_via_argv: bool = False
    prompt_argv_flag: str = "-p"


@dataclass
class MythosConfig:
    """Top-level Mythos configuration."""

    discord_bot_token: str = ""
    discord_guild_id: str = ""
    main_channel_id: str = ""

    workspace_root: str = DEFAULT_WORKSPACE_ROOT
    state_db_path: str = "./mythos-state.json"

    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_planning_rounds: int = DEFAULT_MAX_PLANNING_ROUNDS
    max_handoff_streak: int = DEFAULT_MAX_HANDOFF_STREAK
    max_concurrent_projects: int = 8

    agents: Dict[str, AgentSpec] = field(default_factory=dict)
    runners: Dict[str, RunnerSpec] = field(default_factory=dict)

    # --------------------------------------------------------------- helpers

    def agent_for_role(self, role: str) -> AgentSpec:
        for spec in self.agents.values():
            if spec.role == role:
                return spec
        raise KeyError(f"No agent registered for role {role!r}")

    def agent_for_channel(self, channel: str) -> Optional[AgentSpec]:
        for spec in self.agents.values():
            if spec.channel == channel:
                return spec
        return None

    def runner_for_agent(self, agent_name: str) -> RunnerSpec:
        agent = self.agents[agent_name]
        return self.runners[agent.runner]


# ---------------------------------------------------------------------------
# Loading.
# ---------------------------------------------------------------------------

def _default_runners() -> Dict[str, RunnerSpec]:
    runners: Dict[str, RunnerSpec] = {}
    for name, payload in DEFAULT_RUNNER_COMMANDS.items():
        runners[name] = RunnerSpec(
            name=name,
            command=list(payload["command"]),
            env=dict(payload.get("env", {})),
            prompt_via_argv=(name == "gemini"),
            prompt_argv_flag="-p",
        )
    return runners


def _default_agents() -> Dict[str, AgentSpec]:
    agents: Dict[str, AgentSpec] = {}
    for name in DEFAULT_AGENT_ROLES:
        ident = DEFAULT_AGENT_IDENTITIES[name]
        agents[name] = AgentSpec(
            name=name,
            display_name=ident["display_name"],
            role=ident["role"],
            channel=ident["channel"],
            runner=DEFAULT_RUNNER_BINDING[name],
        )
    return agents


def _read_yaml(path: Optional[Path]) -> Dict[str, Any]:
    if path is None or not path.exists() or yaml is None:
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        return {}
    return data.get("mythos", {}) or {}


def load_config(
    *,
    config_path: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    env: Optional[Dict[str, str]] = None,
) -> MythosConfig:
    """Build a ``MythosConfig`` from defaults + yaml + env + overrides."""

    env = env if env is not None else os.environ
    overrides = overrides or {}

    yaml_section = _read_yaml(Path(config_path)) if config_path else {}

    cfg = MythosConfig(
        agents=_default_agents(),
        runners=_default_runners(),
    )

    # YAML overrides defaults.
    if yaml_section:
        _apply_dict(cfg, yaml_section)

    # Env overrides YAML.
    cfg.discord_bot_token = env.get("DISCORD_BOT_TOKEN", cfg.discord_bot_token)
    cfg.discord_guild_id = env.get("DISCORD_GUILD_ID", cfg.discord_guild_id)
    cfg.main_channel_id = env.get("MYTHOS_MAIN_CHANNEL_ID", cfg.main_channel_id) or env.get(
        "DISCORD_MAIN_CHANNEL_ID", cfg.main_channel_id
    )
    cfg.workspace_root = env.get("MYTHOS_WORKSPACE_ROOT", cfg.workspace_root)
    cfg.state_db_path = env.get("MYTHOS_STATE_PATH", cfg.state_db_path)
    if env.get("MYTHOS_TIMEOUT_SECONDS"):
        cfg.timeout_seconds = int(env["MYTHOS_TIMEOUT_SECONDS"])
    if env.get("MYTHOS_MAX_PLANNING_ROUNDS"):
        cfg.max_planning_rounds = int(env["MYTHOS_MAX_PLANNING_ROUNDS"])
    if env.get("MYTHOS_MAX_HANDOFF_STREAK"):
        cfg.max_handoff_streak = int(env["MYTHOS_MAX_HANDOFF_STREAK"])

    # Per-runner env overrides: API keys etc. applied on top of defaults.
    _apply_runner_env_overrides(cfg, env)

    # Role-to-runner overrides (e.g. BACKEND_RUNNER=codex).
    _apply_runner_binding_overrides(cfg, env)

    # Final: explicit dict overrides.
    if overrides:
        _apply_dict(cfg, overrides)

    return cfg


def _apply_dict(cfg: MythosConfig, data: Dict[str, Any]) -> None:
    for key in (
        "discord_bot_token",
        "discord_guild_id",
        "main_channel_id",
        "workspace_root",
        "state_db_path",
        "timeout_seconds",
        "max_planning_rounds",
        "max_handoff_streak",
        "max_concurrent_projects",
    ):
        if key in data:
            setattr(cfg, key, data[key])

    if "agents" in data and isinstance(data["agents"], dict):
        for name, payload in data["agents"].items():
            spec = cfg.agents.get(name) or AgentSpec(
                name=name,
                display_name=payload.get("display_name", name.title()),
                role=payload.get("role", name),
                channel=payload.get("channel", "planning"),
                runner=payload.get("runner", "claude_code"),
            )
            for k in ("display_name", "role", "channel", "runner", "avatar_url"):
                if k in payload:
                    setattr(spec, k, payload[k])
            cfg.agents[name] = spec

    if "runners" in data and isinstance(data["runners"], dict):
        for name, payload in data["runners"].items():
            spec = cfg.runners.get(name) or RunnerSpec(name=name, command=list(payload.get("command", [])))
            if "command" in payload:
                spec.command = list(payload["command"])
            if "env" in payload:
                spec.env = {**spec.env, **dict(payload["env"])}
            if "prompt_via_argv" in payload:
                spec.prompt_via_argv = bool(payload["prompt_via_argv"])
            if "prompt_argv_flag" in payload:
                spec.prompt_argv_flag = str(payload["prompt_argv_flag"])
            cfg.runners[name] = spec


def _apply_runner_env_overrides(cfg: MythosConfig, env: Dict[str, str]) -> None:
    # Real-host credentials are pulled into the runner env at start time.
    # Dummies remain so the host can hit a local proxy by default.
    if "ANTHROPIC_BASE_URL" in env:
        cfg.runners["claude_code"].env["ANTHROPIC_BASE_URL"] = env["ANTHROPIC_BASE_URL"]
    if "ANTHROPIC_AUTH_TOKEN" in env:
        cfg.runners["claude_code"].env["ANTHROPIC_AUTH_TOKEN"] = env["ANTHROPIC_AUTH_TOKEN"]
    elif "ANTHROPIC_API_KEY" in env:
        cfg.runners["claude_code"].env["ANTHROPIC_AUTH_TOKEN"] = env["ANTHROPIC_API_KEY"]
    if "ANTHROPIC_MODEL" in env:
        cfg.runners["claude_code"].env["ANTHROPIC_MODEL"] = env["ANTHROPIC_MODEL"]

    if "OPENAI_BASE_URL" in env:
        cfg.runners["codex"].env["OPENAI_BASE_URL"] = env["OPENAI_BASE_URL"]
    if "OPENAI_API_KEY" in env:
        cfg.runners["codex"].env["OPENAI_API_KEY"] = env["OPENAI_API_KEY"]

    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if key in env:
            cfg.runners["gemini"].env[key] = env[key]


def _apply_runner_binding_overrides(cfg: MythosConfig, env: Dict[str, str]) -> None:
    role_overrides = {
        "main":       env.get("MAIN_RUNNER"),
        "draft_plan": env.get("DRAFT_PLAN_RUNNER"),
        "review":     env.get("REVIEW_RUNNER"),
        "test":       env.get("TEST_RUNNER"),
        "frontend":   env.get("FRONTEND_RUNNER"),
        "backend":    env.get("BACKEND_RUNNER"),
    }
    for role, runner in role_overrides.items():
        if not runner:
            continue
        if runner not in cfg.runners:
            continue
        for spec in cfg.agents.values():
            if spec.role == role:
                spec.runner = runner


def to_dict(cfg: MythosConfig) -> Dict[str, Any]:
    """Mostly used by tests / debugging — pretty-print the config."""
    out = copy.deepcopy({
        "discord_bot_token": "***" if cfg.discord_bot_token else "",
        "discord_guild_id": cfg.discord_guild_id,
        "main_channel_id": cfg.main_channel_id,
        "workspace_root": cfg.workspace_root,
        "state_db_path": cfg.state_db_path,
        "timeout_seconds": cfg.timeout_seconds,
        "max_planning_rounds": cfg.max_planning_rounds,
        "max_handoff_streak": cfg.max_handoff_streak,
        "max_concurrent_projects": cfg.max_concurrent_projects,
        "agents": {
            n: {"display_name": a.display_name, "role": a.role, "channel": a.channel, "runner": a.runner}
            for n, a in cfg.agents.items()
        },
        "runners": {
            n: {"command": r.command, "env_keys": sorted(r.env.keys()), "prompt_via_argv": r.prompt_via_argv}
            for n, r in cfg.runners.items()
        },
    })
    return out
