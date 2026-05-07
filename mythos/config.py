"""Mythos configuration loader.

Reads ``mythos/config.yaml`` (optional) and overlays env vars. Does not
fail if the config file is absent — every value has a sensible default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import yaml

from mythos.roles import ROLE_BINDINGS, Role, RoleBinding, apply_env_overrides


DEFAULT_WORKSPACE_ROOT = Path.home() / "mythos" / "projects"
DEFAULT_MAIN_CHANNEL_ENV = "MYTHOS_MAIN_CHANNEL_ID"
DEFAULT_GUILD_ENV = "DISCORD_GUILD_ID"
DEFAULT_TOKEN_ENV = "DISCORD_BOT_TOKEN"


@dataclass
class MythosConfig:
    """Top-level Mythos runtime config.

    All fields have defaults; the only required runtime values are the
    Discord bot token and the guild ID, both pulled from the env.
    """

    discord_token: str = ""
    discord_guild_id: int = 0
    main_channel_id: int = 0

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
    if v := os.getenv("MYTHOS_WORKSPACE_ROOT"):
        cfg.workspace_root = Path(os.path.expanduser(v))

    # Resolve role bindings (defaults + env overrides)
    cfg.role_bindings = apply_env_overrides(dict(ROLE_BINDINGS))

    return cfg
