"""Mythos entrypoint: ``python -m mythos``.

Loads config, constructs Discord IO, supervisor, project manager, and
orchestrator, then runs forever.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from mythos.config import load_config
from mythos.discord_io import MultiBotDiscordIO, RealDiscordIO
from mythos.orchestrator import MythosOrchestrator
from mythos.project_manager import ProjectManager
from mythos.supervisor import Supervisor


def _check_required(cfg) -> None:
    missing = []
    if not cfg.discord_token and not cfg.bot_tokens:
        missing.append("DISCORD_BOT_TOKEN or MYTHOS_BOT_TOKENS")
    if not cfg.discord_guild_id:
        missing.append("DISCORD_GUILD_ID")
    if not cfg.main_channel_id:
        missing.append("MYTHOS_MAIN_CHANNEL_ID")
    if missing:
        sys.stderr.write(
            "mythos: missing required env vars: " + ", ".join(missing) + "\n"
            "See mythos/SETUP.md for the operator runbook.\n"
        )
        sys.exit(2)


async def _main() -> None:
    logging.basicConfig(
        level=os.getenv("MYTHOS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    _check_required(cfg)

    if cfg.bot_tokens:
        discord = MultiBotDiscordIO(tokens=cfg.bot_tokens)
    else:
        discord = RealDiscordIO(token=cfg.discord_token)
    pm = ProjectManager(cfg, discord)
    sup = Supervisor(cfg)
    orch = MythosOrchestrator(cfg, discord, pm, sup)
    await orch.run_forever()


if __name__ == "__main__":
    asyncio.run(_main())
