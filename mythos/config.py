"""Mythos configuration.

Loads from a YAML file (default: ``mythos/config.yaml`` next to this package
or ``$MYTHOS_CONFIG``) with env-var overrides for sensitive bits. Defaults
follow the build instructions: Anthropic + OpenAI requests are routed at
``http://127.0.0.1:4141`` and Gemini uses its own preconfigured credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from mythos.agents import AGENT_ROSTER, AgentBackend, AgentRole


# ---- Defaults from the build instructions --------------------------------

DEFAULT_ANTHROPIC_BASE_URL = "http://127.0.0.1:4141"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-4.7-1m-internal"
DEFAULT_ANTHROPIC_AUTH_TOKEN = "dummy"

DEFAULT_OPENAI_BASE_URL = "http://127.0.0.1:4141/v1"
DEFAULT_OPENAI_API_KEY = "dummy"
DEFAULT_CODEX_MODEL = "gpt-5.5"
DEFAULT_CODEX_REASONING_EFFORT = "high"

DEFAULT_GEMINI_MODEL = "gemini-3.1-pro-preview"

DEFAULT_CLAUDE_CODE_EFFORT = "high"

# Subprocess timeout (seconds). Real Claude/Codex/Gemini calls are slow;
# integration tests use much shorter mocks so this default is safe.
DEFAULT_AGENT_TIMEOUT_SECONDS = 900


@dataclass
class AgentConfig:
    """Runtime knobs for one agent role.

    The (role, backend) pair is fixed by AGENT_ROSTER. Everything below is
    user-tunable at runtime.
    """

    role: AgentRole
    model: str
    effort: Optional[str] = None  # claude/codex reasoning effort
    base_url: Optional[str] = None  # anthropic/openai base URL
    auth_env: Dict[str, str] = field(default_factory=dict)
    extra_args: List[str] = field(default_factory=list)
    timeout_seconds: int = DEFAULT_AGENT_TIMEOUT_SECONDS


@dataclass
class MythosConfig:
    """Top-level Mythos configuration."""

    discord_main_channel_id: Optional[int] = None
    discord_guild_id: Optional[int] = None
    discord_bot_token: Optional[str] = None
    workspace_dir: Path = field(default_factory=lambda: Path.home() / ".hermes" / "mythos")
    agents: Dict[AgentRole, AgentConfig] = field(default_factory=dict)

    # Per-channel agent confinement: when True (default), specialist agents
    # only respond in their assigned channel.
    confine_specialists: bool = True

    # Approval keywords that move a project from awaiting_approval -> decomposing.
    approval_keywords: List[str] = field(
        default_factory=lambda: ["approve", "approved", "lgtm", "ship it", "go"]
    )

    # Extra revisions allowed before escalating to user.
    max_revision_rounds: int = 2

    def agent(self, role: AgentRole) -> AgentConfig:
        return self.agents[role]


# ---- Loading --------------------------------------------------------------

def _default_agent_config(role: AgentRole) -> AgentConfig:
    spec = AGENT_ROSTER[role]
    if spec.backend is AgentBackend.CLAUDE_CODE:
        return AgentConfig(
            role=role,
            model=DEFAULT_ANTHROPIC_MODEL,
            effort=DEFAULT_CLAUDE_CODE_EFFORT,
            base_url=DEFAULT_ANTHROPIC_BASE_URL,
            auth_env={
                "ANTHROPIC_BASE_URL": DEFAULT_ANTHROPIC_BASE_URL,
                "ANTHROPIC_AUTH_TOKEN": DEFAULT_ANTHROPIC_AUTH_TOKEN,
                "ANTHROPIC_MODEL": DEFAULT_ANTHROPIC_MODEL,
            },
        )
    if spec.backend is AgentBackend.CODEX:
        return AgentConfig(
            role=role,
            model=DEFAULT_CODEX_MODEL,
            effort=DEFAULT_CODEX_REASONING_EFFORT,
            base_url=DEFAULT_OPENAI_BASE_URL,
            auth_env={
                "OPENAI_BASE_URL": DEFAULT_OPENAI_BASE_URL,
                "OPENAI_API_KEY": DEFAULT_OPENAI_API_KEY,
            },
        )
    if spec.backend is AgentBackend.GEMINI:
        # Gemini CLI uses its own preconfigured credentials. No env overrides
        # by default — the operator's host-level setup handles auth.
        return AgentConfig(
            role=role,
            model=DEFAULT_GEMINI_MODEL,
            effort=None,
            base_url=None,
            auth_env={},
        )
    raise ValueError(f"unknown backend for {role}")


def _default_agents() -> Dict[AgentRole, AgentConfig]:
    return {role: _default_agent_config(role) for role in AGENT_ROSTER}


def load_config(path: Optional[Path] = None) -> MythosConfig:
    """Load Mythos config from YAML, applying env overrides.

    Resolution order for the config path:

    1. Explicit ``path`` argument
    2. ``MYTHOS_CONFIG`` env var
    3. ``mythos/config.yaml`` next to this file
    4. No file — use defaults
    """
    if path is None:
        env_path = os.environ.get("MYTHOS_CONFIG")
        if env_path:
            path = Path(env_path)
        else:
            candidate = Path(__file__).parent / "config.yaml"
            if candidate.exists():
                path = candidate

    raw: Dict = {}
    if path is not None and path.exists():
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    cfg = MythosConfig(agents=_default_agents())

    # Discord wiring
    discord_raw = raw.get("discord", {}) or {}
    cfg.discord_main_channel_id = _coerce_int(
        os.environ.get("MYTHOS_MAIN_CHANNEL_ID")
        or discord_raw.get("main_channel_id")
    )
    cfg.discord_guild_id = _coerce_int(
        os.environ.get("DISCORD_GUILD_ID") or discord_raw.get("guild_id")
    )
    cfg.discord_bot_token = (
        os.environ.get("DISCORD_BOT_TOKEN") or discord_raw.get("token")
    )

    if "workspace_dir" in raw:
        cfg.workspace_dir = Path(os.path.expanduser(raw["workspace_dir"]))
    if env_workspace := os.environ.get("MYTHOS_WORKSPACE_DIR"):
        cfg.workspace_dir = Path(os.path.expanduser(env_workspace))

    if "confine_specialists" in raw:
        cfg.confine_specialists = bool(raw["confine_specialists"])
    if "approval_keywords" in raw:
        cfg.approval_keywords = list(raw["approval_keywords"])
    if "max_revision_rounds" in raw:
        cfg.max_revision_rounds = int(raw["max_revision_rounds"])

    # Per-agent overrides
    agent_overrides = raw.get("agents", {}) or {}
    for role_key, overrides in agent_overrides.items():
        try:
            role = AgentRole(role_key)
        except ValueError:
            continue
        base = cfg.agents[role]
        cfg.agents[role] = replace(
            base,
            model=overrides.get("model", base.model),
            effort=overrides.get("effort", base.effort),
            base_url=overrides.get("base_url", base.base_url),
            auth_env={**base.auth_env, **(overrides.get("env") or {})},
            extra_args=list(overrides.get("extra_args") or base.extra_args),
            timeout_seconds=int(overrides.get("timeout_seconds", base.timeout_seconds)),
        )

    return cfg


def _coerce_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None
