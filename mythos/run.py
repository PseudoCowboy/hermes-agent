"""Mythos runner — wires the live Discord bridge + CLI runtime + orchestrator.

Usage::

    python -m mythos.run

Required env vars (or ``~/.hermes/config.yaml`` ``mythos:`` section):

* ``DISCORD_BOT_TOKEN``
* ``DISCORD_GUILD_ID``
* ``MYTHOS_MAIN_CHANNEL_ID``

Optional:

* ``MYTHOS_WORKSPACE_ROOT`` — where per-project workdirs live (default ``./workspaces``).
* ``MYTHOS_CONFIG`` — explicit path to a yaml config file.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from mythos.cli_runtime import CLIRuntime
from mythos.config import load_config
from mythos.discord_bridge import LiveDiscordBridge
from mythos.orchestrator import Orchestrator
from mythos.store import ProjectStore


async def _main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg = load_config()
    if not cfg.discord_bot_token:
        print("DISCORD_BOT_TOKEN is not set; cannot start Mythos.", file=sys.stderr)
        return 2
    if not cfg.main_channel_id:
        print("MYTHOS_MAIN_CHANNEL_ID is not set; cannot start Mythos.", file=sys.stderr)
        return 2

    bridge = LiveDiscordBridge(
        token=cfg.discord_bot_token,
        guild_id=cfg.discord_guild_id,
        main_channel_id=cfg.main_channel_id,
        category_name=cfg.project_category_name,
    )
    runtime = CLIRuntime(agent_configs=cfg.agents)
    store = ProjectStore(persistence_path=f"{cfg.workspace_root}/mythos-events.jsonl")
    orchestrator = Orchestrator(
        config=cfg, bridge=bridge, runtime=runtime, store=store, workspace_root=cfg.workspace_root
    )

    stop = asyncio.Event()

    def _shutdown(*_: object) -> None:
        stop.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except (NotImplementedError, RuntimeError):
            pass

    await bridge.start()
    logging.getLogger("mythos").info(
        "Mythos online: %d agents, main_channel=%s, guild=%s",
        len(cfg.agents),
        cfg.main_channel_id,
        cfg.discord_guild_id,
    )
    try:
        await stop.wait()
    finally:
        await bridge.close()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
