"""Mythos entrypoint.

Run with::

    python -m mythos.app

Loads MythosConfig, builds a HermesDiscordTransport (or a minimal
discord.py client if hermes-agent's gateway is not already running), and
starts the orchestrator's event loop.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from mythos.config import load_config
from mythos.orchestrator import Orchestrator
from mythos.runners import SubprocessAgentRunner
from mythos.transport import HermesDiscordTransport


async def _amain() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    if not cfg.discord_bot_token:
        print("error: DISCORD_BOT_TOKEN is required", file=sys.stderr)
        return 2
    if cfg.discord_guild_id is None:
        print("error: DISCORD_GUILD_ID is required", file=sys.stderr)
        return 2
    if cfg.discord_main_channel_id is None:
        print("error: MYTHOS_MAIN_CHANNEL_ID is required", file=sys.stderr)
        return 2

    transport = HermesDiscordTransport(
        bot_token=cfg.discord_bot_token,
        guild_id=cfg.discord_guild_id,
    )
    runner = SubprocessAgentRunner()
    orch = Orchestrator(config=cfg, transport=transport, runner=runner)

    stop_event = asyncio.Event()

    def _on_signal() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass  # Windows

    await orch.start()
    try:
        await stop_event.wait()
    finally:
        await orch.stop()
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
