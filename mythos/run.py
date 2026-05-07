"""Run mythos as a long-running service.

Usage:
    python -m mythos.run --config mythos/config.yaml

Required environment:
    DISCORD_BOT_TOKEN
    DISCORD_GUILD_ID
    MYTHOS_MAIN_CHANNEL_ID
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from .config import MythosConfig
from .discord_adapter import DiscordPyAdapter
from .orchestrator import MythosOrchestrator
from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser("mythos")
    parser.add_argument("--config", type=Path, default=None,
                        help="Optional YAML config file.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    cfg = MythosConfig.from_env_and_yaml(args.config)
    if not cfg.discord_bot_token or not cfg.discord_guild_id or not cfg.main_channel_id:
        raise SystemExit(
            "DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, and MYTHOS_MAIN_CHANNEL_ID "
            "are required (see mythos/SETUP.md)."
        )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)

    store = Store(cfg.state_dir / "mythos.sqlite")
    discord = DiscordPyAdapter()
    orch = MythosOrchestrator(cfg, store, discord)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_evt = asyncio.Event()

    def _shutdown():
        stop_evt.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _shutdown)
        except NotImplementedError:
            pass

    async def runner():
        await orch.start()
        await stop_evt.wait()
        await orch.stop()

    try:
        loop.run_until_complete(runner())
    finally:
        store.close()
        loop.close()


if __name__ == "__main__":
    main()
