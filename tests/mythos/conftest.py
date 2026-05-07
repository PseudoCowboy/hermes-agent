"""Pytest fixtures for mythos integration tests.

Builds an Orchestrator wired to FakeDiscordTransport + FakeRunner so every
agent CLI is stubbed and every Discord call is in-memory.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Dict

import pytest

from mythos.config import MythosConfig, _default_agent_profiles
from mythos.discord_bot import FakeDiscordTransport
from mythos.orchestrator import Orchestrator, OrchestratorContext
from mythos.runners import AgentPromptPacket, FakeRunner
from mythos.store import JsonStore
from mythos.workspace import WorkspaceManager


@pytest.fixture
def tmp_state(tmp_path: Path) -> Dict[str, Path]:
    state = tmp_path / "state"
    workspaces = tmp_path / "workspaces"
    state.mkdir()
    workspaces.mkdir()
    return {"state": state, "workspaces": workspaces}


@pytest.fixture
def base_config(tmp_state) -> MythosConfig:
    cfg = MythosConfig(agents=_default_agent_profiles())
    cfg.use_fake_runners = True
    cfg.workspaces_root = str(tmp_state["workspaces"])
    cfg.state_dir = str(tmp_state["state"])
    cfg.discord.main_channel_id = "main_channel_42"
    cfg.discord.guild_id = "guild_1"
    cfg.discord.bot_token = "dummy"
    return cfg


@pytest.fixture
def fake_runner() -> FakeRunner:
    """A pre-wired FakeRunner with happy-path responses for all roles."""
    runner = FakeRunner()

    def draft_plan(packet: AgentPromptPacket) -> str:
        # First call: ask 1 clarifying question. Subsequent calls: produce spec.
        existing = [c for c in runner.calls if c.role == "draft_plan"]
        if len(existing) <= 1:  # this packet is the first
            return (
                "Clarification needed:\n"
                "1. Should the dictionary be bundled or downloaded on first run?\n"
                "(Please answer in this channel.)"
            )
        return (
            "## Design Spec\n\n"
            "### Overview\n"
            "Chrome extension translator with offline dictionary lookup.\n\n"
            "### Functional Requirements\n"
            "- Double-click selects a word; popup shows translation.\n"
            "- Offline dictionary bundled with extension.\n\n"
            "### Frontend\n"
            "- Manifest v3 chrome extension, content script, popup UI.\n\n"
            "### Backend\n"
            "- Local dictionary index (JSON), no network calls.\n\n"
            "### Tests\n"
            "- Unit tests for lookup, integration test for popup rendering.\n"
        )

    def review(packet: AgentPromptPacket) -> str:
        return (
            "Review Verdict: approve\n\n"
            "1. Spec covers core scope and is implementable.\n"
            "2. Consider error UX for missing words.\n"
            "3. Manifest permissions look minimal.\n"
        )

    def frontend(packet: AgentPromptPacket) -> str:
        return (
            "Completion Summary (frontend):\n"
            "- Created manifest.json, content.js, popup.html.\n"
            "- Verified double-click triggers translation."
        )

    def backend(packet: AgentPromptPacket) -> str:
        return (
            "Completion Summary (backend):\n"
            "- Bundled dict.json with 5k entries.\n"
            "- Lookup returns null on miss."
        )

    def test_role(packet: AgentPromptPacket) -> str:
        return (
            "Test Report:\n"
            "- 12 unit tests passed.\n"
            "- 2 integration tests passed.\n"
            "- 0 failures."
        )

    runner.register("draft_plan", draft_plan)
    runner.register("review", review)
    runner.register("frontend", frontend)
    runner.register("backend", backend)
    runner.register("test", test_role)
    return runner


@pytest.fixture
def transport(base_config) -> FakeDiscordTransport:
    return FakeDiscordTransport(main_channel_id=base_config.discord.main_channel_id)


@pytest.fixture
def orchestrator(base_config, transport, fake_runner) -> Orchestrator:
    store = JsonStore(base_config.state_dir)
    workspace = WorkspaceManager(base_config.workspaces_root)
    ctx = OrchestratorContext(
        config=base_config, store=store, workspace=workspace,
        transport=transport, fake_runner=fake_runner,
    )
    orch = Orchestrator(ctx)
    transport.set_message_handler(orch.handle_incoming)
    return orch
