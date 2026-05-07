"""Mythos config loader.

Layered defaults:
    1. ROLE_REGISTRY hard defaults (mythos/roles.py)
    2. config.yaml -> mythos.* keys
    3. Environment variables (DISCORD_*, ANTHROPIC_*, etc.)

Anything mutable (model name, base URL, command path) lives here so the
operator can swap a model or point at a different proxy without touching
package code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from mythos.roles import AgentDefinition, AgentRole, CliKind, ROLE_REGISTRY


@dataclass
class DiscordConfig:
    bot_token: Optional[str] = None
    guild_id: Optional[str] = None
    main_channel_id: Optional[str] = None
    project_channel_prefix: str = "proj-"
    workstream_channel_template: str = "{project}-{workstream}"
    archive_on_complete: bool = False
    allowed_user_ids: List[str] = field(default_factory=list)


@dataclass
class WorkspaceConfig:
    root: str = "~/.hermes/mythos/workspaces"


@dataclass
class MythosConfig:
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    state_dir: str = "~/.hermes/mythos"
    agents: Dict[AgentRole, AgentDefinition] = field(default_factory=dict)
    request_keywords: List[str] = field(default_factory=lambda: [
        "build", "create", "extension", "app", "implement", "make", "develop",
        "translator", "website", "tool", "feature", "let's", "i want", "i need",
        "add", "design", "prototype", "service", "api",
    ])

    def state_dir_path(self) -> Path:
        return Path(os.path.expanduser(self.state_dir))

    def workspace_root_path(self) -> Path:
        return Path(os.path.expanduser(self.workspace.root))

    def agent_for(self, role: AgentRole) -> AgentDefinition:
        return self.agents.get(role) or ROLE_REGISTRY[role]


def _merge_agent(role: AgentRole, override: Dict[str, Any]) -> AgentDefinition:
    base = ROLE_REGISTRY[role]
    name = override.get("name", base.name)
    cli = CliKind(override["cli"]) if "cli" in override else base.cli
    argv = list(override.get("argv", base.argv))
    env = dict(base.env)
    env.update(override.get("env", {}))
    return AgentDefinition(role=role, name=name, cli=cli, argv=argv, env=env)


def load_config(
    config_path: Optional[Path] = None,
    *,
    env: Optional[Dict[str, str]] = None,
) -> MythosConfig:
    env = dict(os.environ if env is None else env)
    cfg = MythosConfig()

    # ── YAML overlay ────────────────────────────────────────────────────
    raw: Dict[str, Any] = {}
    if config_path is None:
        candidates = [
            Path.cwd() / "mythos.yaml",
            Path(os.path.expanduser("~/.hermes/mythos.yaml")),
        ]
        for c in candidates:
            if c.exists():
                config_path = c
                break
    if config_path and Path(config_path).exists():
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    raw_my = raw.get("mythos", raw)  # accept either nested or flat YAML

    discord_raw = raw_my.get("discord", {})
    cfg.discord = DiscordConfig(
        bot_token=discord_raw.get("bot_token"),
        guild_id=str(discord_raw["guild_id"]) if discord_raw.get("guild_id") else None,
        main_channel_id=str(discord_raw["main_channel_id"]) if discord_raw.get("main_channel_id") else None,
        project_channel_prefix=discord_raw.get("project_channel_prefix", "proj-"),
        workstream_channel_template=discord_raw.get("workstream_channel_template", "{project}-{workstream}"),
        archive_on_complete=bool(discord_raw.get("archive_on_complete", False)),
        allowed_user_ids=[str(x) for x in discord_raw.get("allowed_user_ids", [])],
    )

    if "workspace" in raw_my:
        cfg.workspace = WorkspaceConfig(root=raw_my["workspace"].get("root", cfg.workspace.root))
    if "state_dir" in raw_my:
        cfg.state_dir = raw_my["state_dir"]
    if "request_keywords" in raw_my:
        cfg.request_keywords = list(raw_my["request_keywords"])

    # Per-role overrides.
    cfg.agents = {role: ROLE_REGISTRY[role] for role in AgentRole}
    for role_str, override in (raw_my.get("agents") or {}).items():
        try:
            role = AgentRole(role_str)
        except ValueError:
            continue
        cfg.agents[role] = _merge_agent(role, override)

    # ── Env overlay (highest priority for secrets/IDs) ──────────────────
    cfg.discord.bot_token = env.get("DISCORD_BOT_TOKEN", cfg.discord.bot_token)
    cfg.discord.guild_id = env.get("DISCORD_GUILD_ID", cfg.discord.guild_id)
    cfg.discord.main_channel_id = env.get("MYTHOS_MAIN_CHANNEL_ID", cfg.discord.main_channel_id)
    if env.get("MYTHOS_ALLOWED_USER_IDS"):
        cfg.discord.allowed_user_ids = [
            x.strip() for x in env["MYTHOS_ALLOWED_USER_IDS"].split(",") if x.strip()
        ]
    if env.get("MYTHOS_STATE_DIR"):
        cfg.state_dir = env["MYTHOS_STATE_DIR"]
    if env.get("MYTHOS_WORKSPACE_ROOT"):
        cfg.workspace.root = env["MYTHOS_WORKSPACE_ROOT"]

    return cfg
