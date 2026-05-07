"""Configuration for the mythos orchestrator.

Configuration is loaded from environment variables first, then optionally
overlaid with a YAML file. The hardcoded defaults match the spec for
benchmark purposes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - yaml is part of hermes deps
    yaml = None


# ---- Per-agent CLI defaults ----------------------------------------------

CLAUDE_PROXY_URL = "http://127.0.0.1:4141"
CLAUDE_MODEL = "claude-opus-4.7-1m-internal"
CODEX_PROXY_URL = "http://127.0.0.1:4141/v1"
CODEX_MODEL = "gpt-5.5"
GEMINI_MODEL = "gemini-3.1-pro-preview"


@dataclass(frozen=True)
class AgentCLI:
    """Spec for invoking one CLI agent as a subprocess."""

    cli: str  # "claude" | "codex" | "gemini"
    command: list[str]
    env: Dict[str, str] = field(default_factory=dict)

    def with_overrides(self, *, command: Optional[list[str]] = None,
                       env: Optional[Dict[str, str]] = None) -> "AgentCLI":
        return replace(
            self,
            command=list(command) if command is not None else self.command,
            env={**self.env, **(env or {})},
        )


def default_claude_cli() -> AgentCLI:
    return AgentCLI(
        cli="claude",
        command=[
            "claude",
            "--dangerously-skip-permissions",
            "--effort",
            "high",
            "--print",
        ],
        env={
            "ANTHROPIC_BASE_URL": CLAUDE_PROXY_URL,
            "ANTHROPIC_AUTH_TOKEN": "dummy",
            "ANTHROPIC_MODEL": CLAUDE_MODEL,
        },
    )


def default_codex_cli() -> AgentCLI:
    return AgentCLI(
        cli="codex",
        command=[
            "codex",
            "exec",
            "-m",
            CODEX_MODEL,
            "-c",
            "model_reasoning_effort=high",
            "--skip-git-repo-check",
        ],
        env={
            "OPENAI_BASE_URL": CODEX_PROXY_URL,
            "OPENAI_API_KEY": "dummy",
        },
    )


def default_gemini_cli() -> AgentCLI:
    # Note: prompt is appended at invocation time as `-p <prompt>`.
    return AgentCLI(
        cli="gemini",
        command=[
            "gemini",
            "-m",
            GEMINI_MODEL,
            "-y",
        ],
        env={},
    )


@dataclass
class MythosConfig:
    """Top-level mythos configuration."""

    discord_bot_token: str = ""
    discord_guild_id: int = 0
    main_channel_id: int = 0
    workspace_root: Path = Path.home() / ".mythos" / "workspaces"
    state_path: Path = Path.home() / ".mythos" / "state.json"

    # Approval keywords (case-insensitive substring match in plan channel)
    approval_keywords: tuple[str, ...] = ("/approve", "approve", "lgtm", "ship it")
    revision_keywords: tuple[str, ...] = ("/revise", "revise", "changes:", "please change")

    # Per-CLI configuration
    claude_cli: AgentCLI = field(default_factory=default_claude_cli)
    codex_cli: AgentCLI = field(default_factory=default_codex_cli)
    gemini_cli: AgentCLI = field(default_factory=default_gemini_cli)

    # Subprocess execution
    agent_timeout_seconds: int = 600
    max_concurrent_agent_jobs: int = 4

    @staticmethod
    def from_env(overrides: Optional[Dict[str, Any]] = None) -> "MythosConfig":
        cfg = MythosConfig(
            discord_bot_token=os.environ.get("DISCORD_BOT_TOKEN", ""),
            discord_guild_id=int(os.environ.get("DISCORD_GUILD_ID") or 0),
            main_channel_id=int(os.environ.get("MYTHOS_MAIN_CHANNEL_ID") or 0),
        )
        if path := os.environ.get("MYTHOS_WORKSPACE_ROOT"):
            cfg.workspace_root = Path(path).expanduser()
        if path := os.environ.get("MYTHOS_STATE_PATH"):
            cfg.state_path = Path(path).expanduser()

        # Allow overriding model/effort/proxy via env
        claude_env = dict(cfg.claude_cli.env)
        if v := os.environ.get("MYTHOS_CLAUDE_BASE_URL"):
            claude_env["ANTHROPIC_BASE_URL"] = v
        if v := os.environ.get("MYTHOS_CLAUDE_MODEL"):
            claude_env["ANTHROPIC_MODEL"] = v
        cfg.claude_cli = cfg.claude_cli.with_overrides(env=claude_env)

        codex_env = dict(cfg.codex_cli.env)
        if v := os.environ.get("MYTHOS_CODEX_BASE_URL"):
            codex_env["OPENAI_BASE_URL"] = v
        cfg.codex_cli = cfg.codex_cli.with_overrides(env=codex_env)

        # YAML overlay (lowest priority for sensitive vals, highest for model picks)
        yaml_path = os.environ.get("MYTHOS_CONFIG_PATH")
        if yaml_path and yaml is not None and Path(yaml_path).exists():
            with open(yaml_path) as fh:
                raw = yaml.safe_load(fh) or {}
            cfg = _apply_yaml_overlay(cfg, raw)

        if overrides:
            for k, v in overrides.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        return cfg


def _apply_yaml_overlay(cfg: MythosConfig, data: Dict[str, Any]) -> MythosConfig:
    if v := data.get("workspace_root"):
        cfg.workspace_root = Path(str(v)).expanduser()
    if v := data.get("state_path"):
        cfg.state_path = Path(str(v)).expanduser()
    if v := data.get("agent_timeout_seconds"):
        cfg.agent_timeout_seconds = int(v)
    if v := data.get("main_channel_id"):
        cfg.main_channel_id = int(v)

    agents = data.get("agents") or {}
    for key, attr in (("claude", "claude_cli"), ("codex", "codex_cli"), ("gemini", "gemini_cli")):
        spec = agents.get(key)
        if not spec:
            continue
        current: AgentCLI = getattr(cfg, attr)
        cmd = spec.get("command", current.command)
        env = {**current.env, **(spec.get("env") or {})}
        setattr(cfg, attr, AgentCLI(cli=current.cli, command=list(cmd), env=env))
    return cfg
