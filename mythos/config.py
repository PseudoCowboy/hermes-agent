"""Configuration for the Mythos multi-agent system.

The defaults baked in here are the ones the orchestrator will use unless
overridden by config.yaml or environment variables. They are intentionally
explicit so the runtime never silently picks a different model or endpoint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


# Roles in the system. Keep stable — used as keys throughout.
ROLE_ATHENA = "athena"          # main agent
ROLE_PROMETHEUS = "prometheus"  # draft plan agent
ROLE_ARGUS = "argus"            # review agent
ROLE_HEPHAESTUS = "hephaestus"  # test agent
ROLE_APOLLO = "apollo"          # frontend agent
ROLE_ATLAS = "atlas"            # backend agent

ALL_ROLES = (
    ROLE_ATHENA,
    ROLE_PROMETHEUS,
    ROLE_ARGUS,
    ROLE_HEPHAESTUS,
    ROLE_APOLLO,
    ROLE_ATLAS,
)

PROVIDER_CLAUDE = "claude"
PROVIDER_CODEX = "codex"
PROVIDER_GEMINI = "gemini"

# Fixed role -> provider mapping (per spec/agent-role-execution).
ROLE_PROVIDERS: Dict[str, str] = {
    ROLE_ATHENA: PROVIDER_CLAUDE,
    ROLE_PROMETHEUS: PROVIDER_CLAUDE,
    ROLE_ATLAS: PROVIDER_CLAUDE,
    ROLE_ARGUS: PROVIDER_CODEX,
    ROLE_HEPHAESTUS: PROVIDER_CODEX,
    ROLE_APOLLO: PROVIDER_GEMINI,
}


@dataclass
class ProviderConfig:
    """Per-provider CLI invocation defaults."""

    command: List[str]
    env: Dict[str, str] = field(default_factory=dict)
    # If True, the orchestrator passes the prompt as the final positional
    # argument; otherwise it's piped on stdin. Codex/Claude accept stdin;
    # Gemini takes -p <prompt>.
    prompt_as_argument: bool = False
    prompt_arg_flag: Optional[str] = None  # e.g. "-p" for gemini
    # Per-call timeout in seconds.
    timeout_seconds: int = 1800


def _default_claude_provider() -> ProviderConfig:
    return ProviderConfig(
        command=[
            "claude",
            "--dangerously-skip-permissions",
            "--effort",
            "high",
            "--print",
        ],
        env={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4141",
            "ANTHROPIC_AUTH_TOKEN": "dummy",
            "ANTHROPIC_MODEL": "claude-opus-4.7-1m-internal",
        },
        prompt_as_argument=False,
        timeout_seconds=1800,
    )


def _default_codex_provider() -> ProviderConfig:
    return ProviderConfig(
        command=[
            "codex",
            "exec",
            "-m",
            "gpt-5.5",
            "-c",
            "model_reasoning_effort=high",
            "--skip-git-repo-check",
        ],
        env={
            "OPENAI_BASE_URL": "http://127.0.0.1:4141/v1",
            "OPENAI_API_KEY": "dummy",
        },
        prompt_as_argument=False,
        timeout_seconds=1800,
    )


def _default_gemini_provider() -> ProviderConfig:
    return ProviderConfig(
        command=["gemini", "-m", "gemini-3.1-pro-preview", "-y"],
        env={},
        prompt_as_argument=True,
        prompt_arg_flag="-p",
        timeout_seconds=1800,
    )


@dataclass
class DiscordConfig:
    """Discord runtime configuration."""

    bot_token: Optional[str] = None
    guild_id: Optional[int] = None
    main_channel_id: Optional[int] = None
    # Optional: Discord category to nest project channels under.
    project_category_id: Optional[int] = None
    # If true, the bot posts an explicit "approved" command hint after review.
    approval_hint: bool = True


@dataclass
class MythosConfig:
    """Top-level config for the Mythos orchestrator."""

    discord: DiscordConfig = field(default_factory=DiscordConfig)
    providers: Dict[str, ProviderConfig] = field(default_factory=lambda: {
        PROVIDER_CLAUDE: _default_claude_provider(),
        PROVIDER_CODEX: _default_codex_provider(),
        PROVIDER_GEMINI: _default_gemini_provider(),
    })
    # Where per-project state and workspaces live.
    state_dir: Path = field(default_factory=lambda: Path.home() / ".mythos" / "state")
    workspace_root: Path = field(default_factory=lambda: Path.home() / ".mythos" / "workspaces")
    # Per-channel max tokens of agent output to forward to Discord per turn.
    max_message_chars: int = 1900  # Discord hard limit is 2000.

    def provider_for_role(self, role: str) -> ProviderConfig:
        provider_name = ROLE_PROVIDERS[role]
        return self.providers[provider_name]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["state_dir"] = str(self.state_dir)
        d["workspace_root"] = str(self.workspace_root)
        return d


def _coerce_int(val: Any) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def load_config(
    path: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> MythosConfig:
    """Load config from a yaml file with environment variable overrides.

    Env vars override file values. Recognised env vars:

    - DISCORD_BOT_TOKEN
    - DISCORD_GUILD_ID
    - DISCORD_MAIN_CHANNEL_ID
    - DISCORD_PROJECT_CATEGORY_ID
    - MYTHOS_STATE_DIR
    - MYTHOS_WORKSPACE_ROOT
    - MYTHOS_CLAUDE_MODEL, MYTHOS_CLAUDE_BASE_URL
    - MYTHOS_CODEX_MODEL, MYTHOS_CODEX_BASE_URL
    - MYTHOS_GEMINI_MODEL
    """
    env = env if env is not None else dict(os.environ)
    cfg = MythosConfig()

    if path is not None and Path(path).exists():
        raw = yaml.safe_load(Path(path).read_text()) or {}
        d = raw.get("discord", {}) or {}
        cfg.discord.bot_token = d.get("bot_token") or cfg.discord.bot_token
        cfg.discord.guild_id = _coerce_int(d.get("guild_id")) or cfg.discord.guild_id
        cfg.discord.main_channel_id = (
            _coerce_int(d.get("main_channel_id")) or cfg.discord.main_channel_id
        )
        cfg.discord.project_category_id = (
            _coerce_int(d.get("project_category_id"))
            or cfg.discord.project_category_id
        )
        if "state_dir" in raw:
            cfg.state_dir = Path(raw["state_dir"]).expanduser()
        if "workspace_root" in raw:
            cfg.workspace_root = Path(raw["workspace_root"]).expanduser()
        for provider_name, provider_overrides in (raw.get("providers") or {}).items():
            if provider_name not in cfg.providers:
                continue
            existing = cfg.providers[provider_name]
            if "command" in provider_overrides:
                existing.command = list(provider_overrides["command"])
            if "env" in provider_overrides:
                existing.env = {**existing.env, **dict(provider_overrides["env"])}
            if "timeout_seconds" in provider_overrides:
                existing.timeout_seconds = int(provider_overrides["timeout_seconds"])

    cfg.discord.bot_token = env.get("DISCORD_BOT_TOKEN") or cfg.discord.bot_token
    cfg.discord.guild_id = (
        _coerce_int(env.get("DISCORD_GUILD_ID")) or cfg.discord.guild_id
    )
    cfg.discord.main_channel_id = (
        _coerce_int(env.get("DISCORD_MAIN_CHANNEL_ID"))
        or cfg.discord.main_channel_id
    )
    cfg.discord.project_category_id = (
        _coerce_int(env.get("DISCORD_PROJECT_CATEGORY_ID"))
        or cfg.discord.project_category_id
    )
    if env.get("MYTHOS_STATE_DIR"):
        cfg.state_dir = Path(env["MYTHOS_STATE_DIR"]).expanduser()
    if env.get("MYTHOS_WORKSPACE_ROOT"):
        cfg.workspace_root = Path(env["MYTHOS_WORKSPACE_ROOT"]).expanduser()

    claude = cfg.providers[PROVIDER_CLAUDE]
    if env.get("MYTHOS_CLAUDE_BASE_URL"):
        claude.env["ANTHROPIC_BASE_URL"] = env["MYTHOS_CLAUDE_BASE_URL"]
    if env.get("MYTHOS_CLAUDE_MODEL"):
        claude.env["ANTHROPIC_MODEL"] = env["MYTHOS_CLAUDE_MODEL"]

    codex = cfg.providers[PROVIDER_CODEX]
    if env.get("MYTHOS_CODEX_BASE_URL"):
        codex.env["OPENAI_BASE_URL"] = env["MYTHOS_CODEX_BASE_URL"]
    if env.get("MYTHOS_CODEX_MODEL"):
        # Codex CLI takes model via -m flag; rebuild command if user overrides.
        new_cmd: List[str] = []
        skip_next = False
        for token in codex.command:
            if skip_next:
                new_cmd.append(env["MYTHOS_CODEX_MODEL"])
                skip_next = False
            elif token == "-m":
                new_cmd.append(token)
                skip_next = True
            else:
                new_cmd.append(token)
        codex.command = new_cmd

    gemini = cfg.providers[PROVIDER_GEMINI]
    if env.get("MYTHOS_GEMINI_MODEL"):
        new_cmd = []
        skip_next = False
        for token in gemini.command:
            if skip_next:
                new_cmd.append(env["MYTHOS_GEMINI_MODEL"])
                skip_next = False
            elif token == "-m":
                new_cmd.append(token)
                skip_next = True
            else:
                new_cmd.append(token)
        gemini.command = new_cmd

    return cfg
