"""Configuration for Mythos.

Defaults can be overridden by ``config.yaml`` (``mythos`` section) or env vars.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - yaml is a transitive dep of hermes-agent
    yaml = None  # type: ignore[assignment]

from mythos.models import AgentRole


# Default CLI invocation per agent role. Strings here are templates; the actual
# command list is built by ``CLIRuntime`` with prompts piped on stdin (Claude/Codex)
# or as a ``-p`` arg (Gemini).
DEFAULT_AGENT_CLIS: dict[AgentRole, dict[str, Any]] = {
    # Claude Code (Athena, Prometheus, Atlas)
    AgentRole.ATHENA: {
        "runtime": "claude",
        "command": ["claude", "--dangerously-skip-permissions", "--effort", "high", "--print"],
        "model": "claude-opus-4.7-1m-internal",
        "base_url": "http://127.0.0.1:4141",
        "auth_token": "dummy",
    },
    AgentRole.PROMETHEUS: {
        "runtime": "claude",
        "command": ["claude", "--dangerously-skip-permissions", "--effort", "high", "--print"],
        "model": "claude-opus-4.7-1m-internal",
        "base_url": "http://127.0.0.1:4141",
        "auth_token": "dummy",
    },
    AgentRole.ATLAS: {
        "runtime": "claude",
        "command": ["claude", "--dangerously-skip-permissions", "--effort", "high", "--print"],
        "model": "claude-opus-4.7-1m-internal",
        "base_url": "http://127.0.0.1:4141",
        "auth_token": "dummy",
    },
    # Codex (Argus, Hephaestus)
    AgentRole.ARGUS: {
        "runtime": "codex",
        "command": ["codex", "exec", "-m", "gpt-5.5", "-c", "model_reasoning_effort=high", "--skip-git-repo-check"],
        "model": "gpt-5.5",
        "base_url": "http://127.0.0.1:4141/v1",
        "auth_token": "dummy",
    },
    AgentRole.HEPHAESTUS: {
        "runtime": "codex",
        "command": ["codex", "exec", "-m", "gpt-5.5", "-c", "model_reasoning_effort=high", "--skip-git-repo-check"],
        "model": "gpt-5.5",
        "base_url": "http://127.0.0.1:4141/v1",
        "auth_token": "dummy",
    },
    # Gemini (Apollo) — uses host's preconfigured gemini auth, no proxy
    AgentRole.APOLLO: {
        "runtime": "gemini",
        "command": ["gemini", "-m", "gemini-3.1-pro-preview", "-y"],
        "model": "gemini-3.1-pro-preview",
        "base_url": None,
        "auth_token": None,
    },
}


@dataclass
class AgentRuntimeConfig:
    role: AgentRole
    runtime: str  # claude | codex | gemini
    command: list[str]
    model: str
    base_url: str | None = None
    auth_token: str | None = None
    timeout_seconds: int = 600
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass
class MythosConfig:
    """Top-level Mythos config.

    Discord values default to env vars: DISCORD_BOT_TOKEN, DISCORD_GUILD_ID,
    MYTHOS_MAIN_CHANNEL_ID. The agent CLI table is fully configurable via
    config.yaml.
    """

    discord_bot_token: str = ""
    discord_guild_id: int = 0
    main_channel_id: int = 0
    project_category_name: str = "mythos-projects"
    workspace_root: str = ""
    agents: dict[AgentRole, AgentRuntimeConfig] = field(default_factory=dict)
    project_channel_prefix: str = "proj"
    max_concurrent_projects: int = 25
    cli_timeout_seconds: int = 600
    extra: dict[str, Any] = field(default_factory=dict)

    def agent_for(self, role: AgentRole) -> AgentRuntimeConfig:
        if role not in self.agents:
            raise KeyError(f"No CLI config for role {role.value}")
        return self.agents[role]


def _default_agents() -> dict[AgentRole, AgentRuntimeConfig]:
    out: dict[AgentRole, AgentRuntimeConfig] = {}
    for role, spec in DEFAULT_AGENT_CLIS.items():
        out[role] = AgentRuntimeConfig(
            role=role,
            runtime=spec["runtime"],
            command=list(spec["command"]),
            model=spec["model"],
            base_url=spec.get("base_url"),
            auth_token=spec.get("auth_token"),
        )
    return out


def _parse_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        return {}
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        return {}
    section = raw.get("mythos")
    return section if isinstance(section, dict) else {}


def load_config(path: str | Path | None = None) -> MythosConfig:
    """Load config from yaml + env.

    Lookup order:
      1. Explicit ``path`` if given.
      2. ``MYTHOS_CONFIG`` env var.
      3. ``~/.hermes/config.yaml`` (mythos: section).
    """

    cfg = MythosConfig(agents=_default_agents())

    yaml_path: Path | None = None
    if path:
        yaml_path = Path(path)
    elif os.environ.get("MYTHOS_CONFIG"):
        yaml_path = Path(os.environ["MYTHOS_CONFIG"])
    else:
        candidate = Path.home() / ".hermes" / "config.yaml"
        if candidate.exists():
            yaml_path = candidate

    section: dict[str, Any] = {}
    if yaml_path:
        section = _parse_yaml(yaml_path)

    cfg.discord_bot_token = section.get("discord_bot_token") or os.environ.get("DISCORD_BOT_TOKEN", "")
    cfg.discord_guild_id = int(section.get("discord_guild_id") or os.environ.get("DISCORD_GUILD_ID") or 0)
    cfg.main_channel_id = int(section.get("main_channel_id") or os.environ.get("MYTHOS_MAIN_CHANNEL_ID") or 0)
    cfg.project_category_name = section.get("project_category_name") or os.environ.get(
        "MYTHOS_PROJECT_CATEGORY", "mythos-projects"
    )
    cfg.workspace_root = section.get("workspace_root") or os.environ.get(
        "MYTHOS_WORKSPACE_ROOT", str(Path.cwd() / "workspaces")
    )
    cfg.cli_timeout_seconds = int(section.get("cli_timeout_seconds") or os.environ.get("MYTHOS_CLI_TIMEOUT", 600))

    # Per-agent overrides under ``mythos.agents.<role>``.
    overrides = section.get("agents") or {}
    if isinstance(overrides, dict):
        for role_name, override in overrides.items():
            try:
                role = AgentRole(role_name)
            except ValueError:
                continue
            if not isinstance(override, dict):
                continue
            current = cfg.agents.get(role)
            if current is None:
                continue
            if "command" in override and isinstance(override["command"], list):
                current.command = [str(x) for x in override["command"]]
            if "model" in override:
                current.model = str(override["model"])
            if "base_url" in override:
                current.base_url = override["base_url"]
            if "auth_token" in override:
                current.auth_token = override["auth_token"]
            if "timeout_seconds" in override:
                current.timeout_seconds = int(override["timeout_seconds"])
            if "extra_env" in override and isinstance(override["extra_env"], dict):
                current.extra_env = {str(k): str(v) for k, v in override["extra_env"].items()}

    cfg.extra = section.get("extra") or {}
    return cfg
