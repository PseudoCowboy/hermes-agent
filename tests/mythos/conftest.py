"""Shared fixtures for mythos tests.

The unit + integration tests in this dir need:

- a temp ``state_dir`` and ``workspace_root`` so they don't touch ``~/.mythos``;
- a ``FakeDiscordClient`` and ``ScriptedRunner``;
- a fully-wired ``Orchestrator`` ready to receive ``user_says`` calls.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

from mythos.config import MythosConfig
from mythos.discord_client import slugify
from mythos.orchestrator import Orchestrator
from mythos.runners.scripted import ScriptedRunner
from mythos.state import StateStore
from mythos.testing.fake_discord import FakeDiscordClient
from mythos.workspace import WorkspaceManager


@pytest.fixture
def tmp_config(tmp_path: Path) -> MythosConfig:
    cfg = MythosConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.workspace_root = tmp_path / "ws"
    cfg.discord.main_channel_id = 1000
    return cfg


@pytest.fixture
def fake_discord(tmp_config: MythosConfig) -> FakeDiscordClient:
    return FakeDiscordClient(main_channel_id=tmp_config.discord.main_channel_id or 1000)


@pytest.fixture
def scripted_runner() -> ScriptedRunner:
    return ScriptedRunner()


@pytest.fixture
def orchestrator(
    tmp_config: MythosConfig,
    fake_discord: FakeDiscordClient,
    scripted_runner: ScriptedRunner,
) -> Orchestrator:
    return Orchestrator(
        config=tmp_config,
        discord=fake_discord,
        runner=scripted_runner,
        state=StateStore(tmp_config.state_dir),
        workspaces=WorkspaceManager(tmp_config.workspace_root),
    )
