"""Mythos configuration loader.

Reads ``mythos/config.yaml`` (optional) and overlays env vars. Does not
fail if the config file is absent — every value has a sensible default.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import yaml

from mythos.roles import ROLE_BINDINGS, Role, RoleBinding, apply_env_overrides, role_from_string


DEFAULT_WORKSPACE_ROOT = Path.home() / "mythos" / "projects"
DEFAULT_MAIN_CHANNEL_ENV = "MYTHOS_MAIN_CHANNEL_ID"
DEFAULT_PROJECTS_CHANNEL_ENV = "MYTHOS_PROJECTS_CHANNEL_ID"
DEFAULT_GUILD_ENV = "DISCORD_GUILD_ID"
DEFAULT_TOKEN_ENV = "DISCORD_BOT_TOKEN"
BOT_TOKENS_ENV = "MYTHOS_BOT_TOKENS"


@dataclass
class MythosConfig:
    """Top-level Mythos runtime config.

    All fields have defaults; the only required runtime values are the
    Discord bot token and the guild ID, both pulled from the env.
    """

    discord_token: str = ""
    discord_guild_id: int = 0
    main_channel_id: int = 0
    # Index channel where Hermes posts a one-line card per new project
    # (slug, short id, time, workspace path, intake goals).
    projects_channel_id: int = 0

    # Per-role Discord bot tokens. When non-empty, MythosOrchestrator
    # uses MultiBotDiscordIO so each role posts under its own identity.
    # Falls back to single-bot mode (discord_token only) when empty.
    bot_tokens: Dict[Role, str] = field(default_factory=dict)

    workspace_root: Path = field(default_factory=lambda: DEFAULT_WORKSPACE_ROOT)
    role_bindings: Dict[Role, RoleBinding] = field(default_factory=dict)

    # Limits / behavior
    max_concurrent_specialists: int = 6
    max_approval_rounds: int = 2
    cli_timeout_seconds: int = 600
    ack_text: str = (
        "Got it — spinning up a project channel and sending Prometheus "
        "to draft a spec."
    )

    # Channel naming
    category_prefix: str = "mythos"
    general_suffix: str = "general"
    frontend_suffix: str = "frontend"
    backend_suffix: str = "backend"
    test_suffix: str = "test"

    @property
    def index_path(self) -> Path:
        return self.workspace_root / "index.json"


def load_config(path: Optional[Path] = None) -> MythosConfig:
    """Load Mythos config. ``path`` defaults to ``mythos/config.yaml`` next to this module."""
    if path is None:
        path = Path(__file__).parent / "config.yaml"

    raw: dict = {}
    if path.exists():
        with open(path, "r") as f:
            raw = yaml.safe_load(f) or {}

    cfg = MythosConfig()

    if "workspace_root" in raw:
        cfg.workspace_root = Path(os.path.expanduser(raw["workspace_root"]))
    if "max_concurrent_specialists" in raw:
        cfg.max_concurrent_specialists = int(raw["max_concurrent_specialists"])
    if "max_approval_rounds" in raw:
        cfg.max_approval_rounds = int(raw["max_approval_rounds"])
    if "cli_timeout_seconds" in raw:
        cfg.cli_timeout_seconds = int(raw["cli_timeout_seconds"])
    if "ack_text" in raw:
        cfg.ack_text = str(raw["ack_text"])
    if "category_prefix" in raw:
        cfg.category_prefix = str(raw["category_prefix"])

    # Env overrides (always win over config file)
    cfg.discord_token = os.getenv(DEFAULT_TOKEN_ENV, "")
    if v := os.getenv(DEFAULT_GUILD_ENV):
        try:
            cfg.discord_guild_id = int(v)
        except ValueError:
            pass
    if v := os.getenv(DEFAULT_MAIN_CHANNEL_ENV):
        try:
            cfg.main_channel_id = int(v)
        except ValueError:
            pass
    if v := os.getenv(DEFAULT_PROJECTS_CHANNEL_ENV):
        try:
            cfg.projects_channel_id = int(v)
        except ValueError:
            pass
    if v := os.getenv("MYTHOS_WORKSPACE_ROOT"):
        cfg.workspace_root = Path(os.path.expanduser(v))

    # Resolve role bindings (defaults + env overrides)
    cfg.role_bindings = apply_env_overrides(dict(ROLE_BINDINGS))

    # Per-role bot tokens (multi-bot mode). JSON dict keyed by role name.
    if v := os.getenv(BOT_TOKENS_ENV):
        cfg.bot_tokens = _parse_bot_tokens(v, fallback_token=cfg.discord_token)

    return cfg


def _parse_bot_tokens(raw: str, *, fallback_token: str) -> Dict[Role, str]:
    """Parse ``MYTHOS_BOT_TOKENS`` JSON into a {Role: token} dict.

    Unknown role keys are ignored. If ``hermes`` is missing but a legacy
    ``DISCORD_BOT_TOKEN`` is set, Hermes gets the legacy token.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[Role, str] = {}
    for k, v in data.items():
        role = role_from_string(str(k))
        if role is None or not v:
            continue
        out[role] = str(v)
    if Role.HERMES not in out and fallback_token:
        out[Role.HERMES] = fallback_token
    return out
