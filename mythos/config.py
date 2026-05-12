"""Configuration for mythos.

Loads from environment + an optional YAML file. The agent-CLI defaults
match the benchmark spec exactly; they can be overridden per-agent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml


# ---------------------------------------------------------------------------
# Per-agent CLI defaults. These ship as defaults; everything is overridable
# via config.yaml or environment.
# ---------------------------------------------------------------------------

CLAUDE_DEFAULT_CMD = ["claude", "--dangerously-skip-permissions", "--effort", "high", "--print"]
CLAUDE_DEFAULT_ENV = {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4141",
    "ANTHROPIC_AUTH_TOKEN": "dummy",
    "ANTHROPIC_MODEL": "claude-opus-4.7-1m-internal",
}

CODEX_DEFAULT_CMD = [
    "codex", "exec",
    "-m", "gpt-5.5",
    "-c", "model_reasoning_effort=high",
    "--dangerously-bypass-approvals-and-sandbox",
    "--skip-git-repo-check",
]
CODEX_DEFAULT_ENV = {
    "OPENAI_BASE_URL": "http://127.0.0.1:4141/v1",
    "OPENAI_API_KEY": "dummy",
}

# Gemini takes the prompt as a flag rather than stdin.
GEMINI_DEFAULT_CMD = ["gemini", "-m", "gemini-3.1-pro-preview", "-y"]
GEMINI_DEFAULT_ENV: Dict[str, str] = {}


AGENT_ROLE_NAMES = (
    "athena",
    "prometheus",
    "argus",
    "hephaestus",
    "apollo",
    "atlas",
)


ROLE_BOT_TOKEN_ENV_VARS: Dict[str, tuple[str, ...]] = {
    role: (
        f"MYTHOS_{role.upper()}_BOT_TOKEN",
        f"DISCORD_{role.upper()}_BOT_TOKEN",
    )
    for role in AGENT_ROLE_NAMES
}


@dataclass
class AgentCLIConfig:
    """How to invoke one underlying coding-agent CLI."""

    kind: str  # "claude" | "codex" | "gemini"
    command: List[str]
    env: Dict[str, str] = field(default_factory=dict)
    # gemini takes prompt as flag (-p <prompt>); claude/codex take it on stdin.
    prompt_via: str = "stdin"  # "stdin" | "flag:-p"
    timeout_seconds: int = 600

    @classmethod
    def claude(cls) -> "AgentCLIConfig":
        return cls(kind="claude", command=list(CLAUDE_DEFAULT_CMD), env=dict(CLAUDE_DEFAULT_ENV))

    @classmethod
    def codex(cls) -> "AgentCLIConfig":
        return cls(kind="codex", command=list(CODEX_DEFAULT_CMD), env=dict(CODEX_DEFAULT_ENV))

    @classmethod
    def gemini(cls) -> "AgentCLIConfig":
        return cls(
            kind="gemini",
            command=list(GEMINI_DEFAULT_CMD),
            env=dict(GEMINI_DEFAULT_ENV),
            prompt_via="flag:-p",
        )


@dataclass
class MythosConfig:
    """Top-level mythos configuration."""

    discord_bot_token: str = ""
    discord_guild_id: int = 0
    main_channel_id: int = 0
    # Where to write per-project working dirs and the state DB.
    state_dir: Path = Path("./mythos_state")
    # Maximum revise/review cycles before forcing user input.
    max_review_iterations: int = 2
    # Per-agent CLI configs (one per role).
    agent_cli: Dict[str, AgentCLIConfig] = field(default_factory=dict)
    # Optional send-only Discord bot tokens by Mythos role. The primary
    # DISCORD_BOT_TOKEN remains the only inbound/admin client.
    discord_role_bot_tokens: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env_and_yaml(cls, yaml_path: Optional[Path] = None) -> "MythosConfig":
        cfg = cls()
        # Defaults: roles -> CLI kind.
        cfg.agent_cli = {
            "athena": AgentCLIConfig.claude(),
            "prometheus": AgentCLIConfig.claude(),
            "atlas": AgentCLIConfig.claude(),
            "argus": AgentCLIConfig.codex(),
            "hephaestus": AgentCLIConfig.codex(),
            "apollo": AgentCLIConfig.gemini(),
        }

        # Optional YAML overrides.
        if yaml_path and yaml_path.exists():
            data = yaml.safe_load(yaml_path.read_text()) or {}
            if "state_dir" in data:
                cfg.state_dir = Path(data["state_dir"]).expanduser()
            if "max_review_iterations" in data:
                cfg.max_review_iterations = int(data["max_review_iterations"])
            for role, token in (data.get("discord_role_bots") or {}).items():
                role_lc = str(role).lower().strip()
                token_s = str(token).strip() if token is not None else ""
                if role_lc in ROLE_BOT_TOKEN_ENV_VARS and token_s:
                    cfg.discord_role_bot_tokens[role_lc] = token_s
            for role, override in (data.get("agents") or {}).items():
                role_lc = role.lower()
                if role_lc not in cfg.agent_cli:
                    continue
                ac = cfg.agent_cli[role_lc]
                if "command" in override:
                    ac.command = list(override["command"])
                if "env" in override:
                    ac.env.update({str(k): str(v) for k, v in override["env"].items()})
                if "prompt_via" in override:
                    ac.prompt_via = override["prompt_via"]
                if "timeout_seconds" in override:
                    ac.timeout_seconds = int(override["timeout_seconds"])

        # Env overrides win last.
        cfg.discord_bot_token = os.environ.get("DISCORD_BOT_TOKEN", cfg.discord_bot_token)
        if os.environ.get("DISCORD_GUILD_ID"):
            cfg.discord_guild_id = int(os.environ["DISCORD_GUILD_ID"])
        if os.environ.get("MYTHOS_MAIN_CHANNEL_ID"):
            cfg.main_channel_id = int(os.environ["MYTHOS_MAIN_CHANNEL_ID"])
        if os.environ.get("MYTHOS_STATE_DIR"):
            cfg.state_dir = Path(os.environ["MYTHOS_STATE_DIR"]).expanduser()
        if os.environ.get("MYTHOS_MAX_ITERATIONS"):
            cfg.max_review_iterations = int(os.environ["MYTHOS_MAX_ITERATIONS"])

        for role, env_names in ROLE_BOT_TOKEN_ENV_VARS.items():
            for env_name in env_names:
                token = os.environ.get(env_name)
                if token and token.strip():
                    cfg.discord_role_bot_tokens[role] = token.strip()
                    break

        # Per-agent env-var endpoint overrides.
        _apply_env_override(cfg.agent_cli["athena"], "MYTHOS_CLAUDE")
        _apply_env_override(cfg.agent_cli["prometheus"], "MYTHOS_CLAUDE")
        _apply_env_override(cfg.agent_cli["atlas"], "MYTHOS_CLAUDE")
        _apply_env_override(cfg.agent_cli["argus"], "MYTHOS_CODEX")
        _apply_env_override(cfg.agent_cli["hephaestus"], "MYTHOS_CODEX")
        _apply_env_override(cfg.agent_cli["apollo"], "MYTHOS_GEMINI")

        return cfg


def _apply_env_override(ac: AgentCLIConfig, prefix: str) -> None:
    """Apply MYTHOS_<KIND>_BASE_URL / MYTHOS_<KIND>_MODEL env overrides."""
    base = os.environ.get(f"{prefix}_BASE_URL")
    if base:
        if ac.kind == "claude":
            ac.env["ANTHROPIC_BASE_URL"] = base
        elif ac.kind == "codex":
            ac.env["OPENAI_BASE_URL"] = base
    model = os.environ.get(f"{prefix}_MODEL")
    if model:
        if ac.kind == "claude":
            ac.env["ANTHROPIC_MODEL"] = model
        elif ac.kind == "codex":
            for i, tok in enumerate(ac.command):
                if tok == "-m" and i + 1 < len(ac.command):
                    ac.command[i + 1] = model
                    break
        elif ac.kind == "gemini":
            for i, tok in enumerate(ac.command):
                if tok == "-m" and i + 1 < len(ac.command):
                    ac.command[i + 1] = model
                    break
