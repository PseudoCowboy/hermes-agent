"""Mythos bot launcher.

Run with: `python -m mythos.bot` (after configuring env vars).
Or invoke `from mythos.bot import run_bot` programmatically.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from mythos.config import load_config
from mythos.discord_bot import FakeDiscordTransport, RealDiscordTransport
from mythos.orchestrator import Orchestrator, OrchestratorContext
from mythos.runners import FakeRunner
from mythos.store import JsonStore
from mythos.workspace import WorkspaceManager


logger = logging.getLogger("mythos.bot")


def build_orchestrator(
    config_path: str | None = None, dry_run: bool = False
) -> Orchestrator:
    cfg = load_config(config_path=config_path)
    if dry_run:
        cfg.use_fake_runners = True

    store = JsonStore(cfg.state_dir)
    workspace = WorkspaceManager(cfg.workspaces_root)

    if dry_run:
        transport = FakeDiscordTransport(
            main_channel_id=cfg.discord.main_channel_id or "main"
        )
    else:
        transport = RealDiscordTransport(
            bot_token=cfg.discord.bot_token,
            guild_id=cfg.discord.guild_id,
            main_channel_id=cfg.discord.main_channel_id,
        )

    fake_runner = FakeRunner() if cfg.use_fake_runners else None
    ctx = OrchestratorContext(
        config=cfg, store=store, workspace=workspace,
        transport=transport, fake_runner=fake_runner,
    )
    return Orchestrator(ctx)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Mythos Discord bot")
    parser.add_argument("--config", help="Path to config.yaml", default=None)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Use FakeDiscordTransport + FakeRunner (no Discord, no CLIs)",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    orch = build_orchestrator(config_path=args.config, dry_run=args.dry_run)
    if not args.dry_run:
        cfg = orch.config
        missing = []
        if not cfg.discord.bot_token:
            missing.append("DISCORD_BOT_TOKEN")
        if not cfg.discord.guild_id:
            missing.append("DISCORD_GUILD_ID")
        if not cfg.discord.main_channel_id:
            missing.append("MYTHOS_MAIN_CHANNEL_ID")
        if missing:
            logger.error("Missing required config: %s", ", ".join(missing))
            return 2

    try:
        asyncio.run(orch.start())
    except KeyboardInterrupt:
        logger.info("shutting down")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
