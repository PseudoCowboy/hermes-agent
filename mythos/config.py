"""Configuration loader for mythos.

Reads from (in order of precedence):
  1. Programmatic overrides
  2. Environment variables
  3. config.yaml at MYTHOS_CONFIG (default: mythos/config.yaml or mythos/config.example.yaml)
  4. Built-in defaults

The benchmark-mandated agent CLI defaults live here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - yaml optional in tests
    yaml = None


# --- Built-in defaults (benchmark-mandated) ---------------------------------

DEFAULT_CLAUDE_CODE_COMMAND = [
    "claude",
    "--dangerously-skip-permissions",
    "--effort", "high",
    "--print",
]
DEFAULT_CLAUDE_CODE_ENV = {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4141",
    "ANTHROPIC_AUTH_TOKEN": "dummy",
    "ANTHROPIC_MODEL": "claude-opus-4.7-1m-internal",
}

DEFAULT_CODEX_COMMAND = [
    "codex", "exec",
    "-m", "gpt-5.5",
    "-c", "model_reasoning_effort=high",
    "--skip-git-repo-check",
]
DEFAULT_CODEX_ENV = {
    "OPENAI_BASE_URL": "http://127.0.0.1:4141/v1",
    "OPENAI_API_KEY": "dummy",
}

DEFAULT_GEMINI_COMMAND = [
    "gemini",
    "-m", "gemini-3.1-pro-preview",
    "-y",
]
DEFAULT_GEMINI_ENV: Dict[str, str] = {}


@dataclass
class AgentProfile:
    """Configuration for one specialist agent role."""
    role: str                           # AgentRole value
    codename: str                       # Athena, Prometheus, ...
    provider: str                       # "claude" | "codex" | "gemini"
    command: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 1200


@dataclass
class DiscordConfig:
    bot_token: str = ""
    guild_id: str = ""
    main_channel_id: str = ""
    project_category_id: str = ""       # optional Discord category for projects
    approval_keywords: List[str] = field(
        default_factory=lambda: ["approve", "approved", "lgtm", "ship it", "/approve"]
    )


@dataclass
class MythosConfig:
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    workspaces_root: str = "./mythos_workspaces"
    state_dir: str = "./mythos_state"
    artifacts_root: str = "./mythos_workspaces"  # under workspace path
    max_review_rounds: int = 2
    agents: Dict[str, AgentProfile] = field(default_factory=dict)
    # If true the orchestrator does not actually exec CLIs; instead it calls
    # the registered fake_runner. Useful for tests.
    use_fake_runners: bool = False

    def agent(self, role: str) -> AgentProfile:
        if role not in self.agents:
            raise KeyError(f"No agent profile configured for role '{role}'")
        return self.agents[role]


def _default_agent_profiles() -> Dict[str, AgentProfile]:
    return {
        "main": AgentProfile(
            role="main", codename="Athena", provider="claude",
            command=list(DEFAULT_CLAUDE_CODE_COMMAND),
            env=dict(DEFAULT_CLAUDE_CODE_ENV),
        ),
        "draft_plan": AgentProfile(
            role="draft_plan", codename="Prometheus", provider="claude",
            command=list(DEFAULT_CLAUDE_CODE_COMMAND),
            env=dict(DEFAULT_CLAUDE_CODE_ENV),
        ),
        "review": AgentProfile(
            role="review", codename="Argus", provider="codex",
            command=list(DEFAULT_CODEX_COMMAND),
            env=dict(DEFAULT_CODEX_ENV),
        ),
        "test": AgentProfile(
            role="test", codename="Hephaestus", provider="codex",
            command=list(DEFAULT_CODEX_COMMAND),
            env=dict(DEFAULT_CODEX_ENV),
        ),
        "frontend": AgentProfile(
            role="frontend", codename="Apollo", provider="gemini",
            command=list(DEFAULT_GEMINI_COMMAND),
            env=dict(DEFAULT_GEMINI_ENV),
        ),
        "backend": AgentProfile(
            role="backend", codename="Atlas", provider="claude",
            command=list(DEFAULT_CLAUDE_CODE_COMMAND),
            env=dict(DEFAULT_CLAUDE_CODE_ENV),
        ),
    }


def _apply_yaml_overrides(cfg: MythosConfig, raw: Dict[str, Any]) -> None:
    discord_raw = raw.get("discord") or {}
    if "bot_token" in discord_raw:
        cfg.discord.bot_token = str(discord_raw["bot_token"])
    if "guild_id" in discord_raw:
        cfg.discord.guild_id = str(discord_raw["guild_id"])
    if "main_channel_id" in discord_raw:
        cfg.discord.main_channel_id = str(discord_raw["main_channel_id"])
    if "project_category_id" in discord_raw:
        cfg.discord.project_category_id = str(discord_raw["project_category_id"])
    if "approval_keywords" in discord_raw and isinstance(discord_raw["approval_keywords"], list):
        cfg.discord.approval_keywords = [str(k) for k in discord_raw["approval_keywords"]]

    if "workspaces_root" in raw:
        cfg.workspaces_root = str(raw["workspaces_root"])
    if "state_dir" in raw:
        cfg.state_dir = str(raw["state_dir"])
    if "max_review_rounds" in raw:
        cfg.max_review_rounds = int(raw["max_review_rounds"])
    if "use_fake_runners" in raw:
        cfg.use_fake_runners = bool(raw["use_fake_runners"])

    agents_raw = raw.get("agents") or {}
    for role, overrides in agents_raw.items():
        if role not in cfg.agents:
            continue
        prof = cfg.agents[role]
        if "command" in overrides and isinstance(overrides["command"], list):
            prof.command = [str(x) for x in overrides["command"]]
        if "env" in overrides and isinstance(overrides["env"], dict):
            prof.env.update({str(k): str(v) for k, v in overrides["env"].items()})
        if "timeout_seconds" in overrides:
            prof.timeout_seconds = int(overrides["timeout_seconds"])
        if "codename" in overrides:
            prof.codename = str(overrides["codename"])


def _apply_env_overrides(cfg: MythosConfig) -> None:
    cfg.discord.bot_token = os.getenv("DISCORD_BOT_TOKEN", cfg.discord.bot_token)
    cfg.discord.guild_id = os.getenv("DISCORD_GUILD_ID", cfg.discord.guild_id)
    cfg.discord.main_channel_id = os.getenv(
        "MYTHOS_MAIN_CHANNEL_ID", cfg.discord.main_channel_id
    )
    cfg.discord.project_category_id = os.getenv(
        "MYTHOS_PROJECT_CATEGORY_ID", cfg.discord.project_category_id
    )
    cfg.workspaces_root = os.getenv("MYTHOS_WORKSPACES_ROOT", cfg.workspaces_root)
    cfg.state_dir = os.getenv("MYTHOS_STATE_DIR", cfg.state_dir)

    # Per-role command/env overrides via MYTHOS_<ROLE>_MODEL etc.
    for role, prof in cfg.agents.items():
        upper = role.upper()
        # model override (replace value following -m / --model when present)
        model_env = os.getenv(f"MYTHOS_{upper}_MODEL")
        if model_env:
            _replace_model_in_command(prof.command, model_env)
        # base_url override
        base_url_env = os.getenv(f"MYTHOS_{upper}_BASE_URL")
        if base_url_env:
            if prof.provider == "claude":
                prof.env["ANTHROPIC_BASE_URL"] = base_url_env
            elif prof.provider == "codex":
                prof.env["OPENAI_BASE_URL"] = base_url_env
        # auth token
        token_env = os.getenv(f"MYTHOS_{upper}_AUTH_TOKEN")
        if token_env:
            if prof.provider == "claude":
                prof.env["ANTHROPIC_AUTH_TOKEN"] = token_env
            elif prof.provider == "codex":
                prof.env["OPENAI_API_KEY"] = token_env

    if os.getenv("MYTHOS_USE_FAKE_RUNNERS", "").lower() in {"1", "true", "yes"}:
        cfg.use_fake_runners = True


def _replace_model_in_command(cmd: List[str], new_model: str) -> None:
    for i, tok in enumerate(cmd):
        if tok in ("-m", "--model") and i + 1 < len(cmd):
            cmd[i + 1] = new_model
            return
    # No existing flag: append for codex/gemini-style commands
    cmd.extend(["-m", new_model])


def load_config(
    config_path: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> MythosConfig:
    cfg = MythosConfig(agents=_default_agent_profiles())

    path = config_path or os.getenv("MYTHOS_CONFIG")
    if path and Path(path).exists() and yaml is not None:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        _apply_yaml_overrides(cfg, raw)

    _apply_env_overrides(cfg)

    if overrides:
        _apply_yaml_overrides(cfg, overrides)

    return cfg
