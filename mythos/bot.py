"""Mythos bot entrypoint.

Run with:

    python -m mythos.bot --config mythos/config.yaml

or with env vars only (see SETUP.md).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from mythos.config import load_config
from mythos.discord_py_client import DiscordPyClient
from mythos.orchestrator import Orchestrator
from mythos.runners import SubprocessRunner


def _parse_args(argv):
    p = argparse.ArgumentParser(description="Mythos multi-agent Discord bot")
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to mythos config.yaml (env vars still override).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default INFO).",
    )
    return p.parse_args(argv)


async def _async_main(args) -> int:
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(args.config)
    if not cfg.discord.bot_token:
        print("ERROR: DISCORD_BOT_TOKEN not set (or no bot_token in config).", file=sys.stderr)
        return 2
    if not cfg.discord.guild_id or not cfg.discord.main_channel_id:
        print(
            "ERROR: DISCORD_GUILD_ID and DISCORD_MAIN_CHANNEL_ID must be set.",
            file=sys.stderr,
        )
        return 2

    client = DiscordPyClient(
        token=cfg.discord.bot_token,
        guild_id=cfg.discord.guild_id,
        main_channel_id=cfg.discord.main_channel_id,
        project_category_id=cfg.discord.project_category_id,
    )
    runner = SubprocessRunner(cfg)
    Orchestrator(cfg, client, runner)  # registers itself on the client
    try:
        await client.start()
    finally:
        await client.close()
    return 0


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
