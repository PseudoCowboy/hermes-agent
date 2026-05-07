"""Mythos entrypoint: ``python -m mythos`` boots the real Discord bot.

The orchestrator + runner + adapter are wired here. Tests bypass this
entrypoint and construct an Orchestrator directly with fakes.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from mythos.config import load_config
from mythos.discord_adapter import DiscordClient
from mythos.orchestrator import Orchestrator
from mythos.runners import SubprocessRunner

logger = logging.getLogger("mythos")


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config_path = None
    if argv and len(argv) > 1:
        config_path = Path(argv[1])

    cfg = load_config(config_path)
    if not cfg.discord.bot_token:
        print("ERROR: DISCORD_BOT_TOKEN not configured (env or config).", file=sys.stderr)
        return 2
    if not cfg.discord.guild_id:
        print("ERROR: DISCORD_GUILD_ID not configured (env or config).", file=sys.stderr)
        return 2
    if not cfg.discord.main_channel_id:
        print("ERROR: MYTHOS_MAIN_CHANNEL_ID not configured (env or config).", file=sys.stderr)
        return 2

    discord = DiscordClient(
        bot_token=cfg.discord.bot_token,
        guild_id=int(cfg.discord.guild_id),
    )
    runner = SubprocessRunner()
    orchestrator = Orchestrator(config=cfg, discord=discord, runner=runner)

    logger.info("Mythos starting; main channel=%s guild=%s", cfg.discord.main_channel_id, cfg.discord.guild_id)
    discord.start()  # blocks until bot dies (background thread keeps running)
    try:
        # Keep main thread alive while the discord bot thread runs.
        import threading
        forever = threading.Event()
        forever.wait()
    except KeyboardInterrupt:
        logger.info("Mythos shutting down")
        orchestrator.shutdown()
        discord.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
