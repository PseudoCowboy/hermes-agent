"""Mythos entry point.

Run: `python -m mythos`

Reads config from environment variables (see mythos/.env.example) and
connects to Discord.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from .agent_runner import AgentRunner
from .config import MythosConfig
from .discord_bot import DiscordpyClient
from .orchestrator import Orchestrator
from .state import ProjectStore
from .workspace import WorkspaceManager


log = logging.getLogger("mythos")


async def amain() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = MythosConfig.from_env()
    if not cfg.discord_bot_token:
        log.error("DISCORD_BOT_TOKEN is required")
        return 2
    if not cfg.discord_guild_id:
        log.error("DISCORD_GUILD_ID is required")
        return 2
    if not cfg.main_channel_id:
        log.error("MYTHOS_MAIN_CHANNEL_ID is required")
        return 2

    store = ProjectStore(cfg.state_path)
    ws = WorkspaceManager(cfg.workspace_root)
    runner = AgentRunner(cfg)
    discord_client = DiscordpyClient(
        token=cfg.discord_bot_token,
        guild_id=cfg.discord_guild_id,
        main_channel_id=cfg.main_channel_id,
    )
    Orchestrator(
        config=cfg,
        store=store,
        discord=discord_client,
        agent_runner=runner,
        workspace_manager=ws,
        guild_id=cfg.discord_guild_id,
    )
    log.info("Mythos starting; main channel = %d, guild = %d",
             cfg.main_channel_id, cfg.discord_guild_id)
    await discord_client.start()
    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
