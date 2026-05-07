"""Mythos entrypoint: ``python -m mythos.run``.

Wires up config, project store, agent registry with subprocess runners,
and starts the production Discord adapter. Operators normally run this
directly; integration tests build the same pieces by hand with the
``InMemoryDiscord`` and ``FakeRunner`` substitutes.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from typing import Dict

from mythos.agents.registry import AgentRegistry
from mythos.config import load_config
from mythos.orchestrator import Orchestrator
from mythos.projects import ProjectStore
from mythos.runners.base import AgentRunner
from mythos.runners.subprocess_runner import build_runner_from_spec


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mythos multi-agent Discord bot")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = load_config(config_path=args.config)
    if not cfg.discord_bot_token:
        print("DISCORD_BOT_TOKEN is required", file=sys.stderr)
        return 2
    if not cfg.discord_guild_id:
        print("DISCORD_GUILD_ID is required", file=sys.stderr)
        return 2
    if not cfg.main_channel_id:
        print("MYTHOS_MAIN_CHANNEL_ID is required", file=sys.stderr)
        return 2

    runners: Dict[str, AgentRunner] = {
        name: build_runner_from_spec(spec) for name, spec in cfg.runners.items()
    }
    store = ProjectStore(cfg.state_db_path, cfg.workspace_root)
    registry = AgentRegistry(cfg, runners)

    # Lazy import so the test suite doesn't need discord.py.
    from mythos.discord_real import DiscordPyAdapter

    adapter = DiscordPyAdapter(token=cfg.discord_bot_token, guild_id=int(cfg.discord_guild_id))
    orchestrator = Orchestrator(config=cfg, store=store, registry=registry, discord=adapter)
    adapter.add_listener(orchestrator.handle_message)

    adapter.start()
    if not adapter.wait_until_ready(timeout=30):
        print("Discord client did not become ready in 30s", file=sys.stderr)
        return 1

    stop = {"flag": False}

    def _shutdown(*_args):
        stop["flag"] = True
        adapter.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while not stop["flag"]:
        time.sleep(1.0)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
