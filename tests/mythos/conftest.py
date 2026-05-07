"""Shared fixtures for the mythos test suite.

Uses a `FakeAgentExecutor` that returns scripted responses for each role
so we can exercise the full happy path without invoking real CLIs.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import pytest

from mythos.agent_runner import AgentRunner, SubprocessExecutor
from mythos.config import MythosConfig
from mythos.discord_ops import FakeDiscordClient
from mythos.orchestrator import Orchestrator
from mythos.roles import Role
from mythos.state import ProjectStore
from mythos.workspace import WorkspaceManager


# ---------------------------------------------------------------------------


@dataclass
class FakeAgentExecutor:
    """Subprocess executor double.

    Maps argv[0] (the CLI binary) to a list of canned (returncode, stdout,
    stderr) tuples; pops one each time it's called. This lets a test script
    a sequence of agent responses regardless of role.
    """

    by_cli: Dict[str, List[Tuple[int, str, str]]] = field(default_factory=dict)
    calls: List[dict] = field(default_factory=list)

    def push(self, cli: str, stdout: str, *, returncode: int = 0,
             stderr: str = "") -> None:
        self.by_cli.setdefault(cli, []).append((returncode, stdout, stderr))

    async def __call__(
        self,
        argv: List[str],
        stdin_text: Optional[str],
        env: Dict[str, str],
        cwd: Path,
        timeout: float,
    ) -> Tuple[int, str, str]:
        cli = argv[0]
        self.calls.append({
            "cli": cli, "argv": argv, "stdin_text": stdin_text,
            "env": env, "cwd": str(cwd), "timeout": timeout,
        })
        queue = self.by_cli.get(cli) or []
        if not queue:
            return 0, f"[fake-{cli}] no scripted response\nSTATUS: completed\n", ""
        return queue.pop(0)


# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_state_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "mythos.json"


@pytest.fixture
def tmp_workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    return root


@pytest.fixture
def fake_executor() -> FakeAgentExecutor:
    return FakeAgentExecutor()


@pytest.fixture
def fake_discord() -> FakeDiscordClient:
    return FakeDiscordClient()


@pytest.fixture
def mythos_config(tmp_state_path: Path, tmp_workspace_root: Path) -> MythosConfig:
    cfg = MythosConfig(
        discord_bot_token="test-token",
        discord_guild_id=42,
        main_channel_id=1000,
        workspace_root=tmp_workspace_root,
        state_path=tmp_state_path,
        agent_timeout_seconds=5,
        max_concurrent_agent_jobs=4,
    )
    return cfg


@pytest.fixture
def orchestrator(mythos_config, fake_discord, fake_executor,
                 tmp_workspace_root, tmp_state_path):
    runner = AgentRunner(mythos_config, executor=fake_executor)
    store = ProjectStore(tmp_state_path)
    ws = WorkspaceManager(tmp_workspace_root)
    orch = Orchestrator(
        config=mythos_config,
        store=store,
        discord=fake_discord,
        agent_runner=runner,
        workspace_manager=ws,
        guild_id=mythos_config.discord_guild_id,
    )
    return orch


# ---------------------------------------------------------------------------


def script_happy_path(executor: FakeAgentExecutor, *,
                      include_revision: bool = False) -> None:
    """Push canned outputs for a single end-to-end project run."""

    # -- Athena intake (claude) ------------------------------------------
    executor.push("claude",
                  "I'm Athena. Accepted the project. Title: Translator Extension. "
                  "Goal: build a Chrome extension that translates double-clicked "
                  "text using a built-in dictionary.\nSTATUS: completed\n")

    # -- Prometheus clarify (claude) -------------------------------------
    executor.push("claude",
                  "QUESTION: 1. Which language pair?\n"
                  "QUESTION: 2. Offline-only or fall back to an online API?\n"
                  "QUESTION: 3. Any UI design preferences?\n"
                  "STATUS: completed\n")

    if include_revision:
        # First spec draft (will get rejected)
        executor.push("claude",
                      "1. Title: Chrome Translator (draft v1)\n2. Goal: ...\n"
                      "STATUS: completed\n")
        # First Argus review
        executor.push("codex",
                      "BLOCKING: missing browser permissions section.\n"
                      "STATUS: completed\n")
        # First Athena present-for-approval
        executor.push("claude",
                      "Spec v1 is ready for your review. Reply approve or revise.\n"
                      "STATUS: completed\n")

    # -- Prometheus draft spec (final) -----------------------------------
    executor.push("claude",
                  "1. Title: Chrome Translator Extension\n"
                  "2. Goal: Translate selected text via built-in dictionary.\n"
                  "3. Target Users: language learners.\n"
                  "4. Functional Requirements: dblclick handler, dictionary lookup, popup.\n"
                  "5. Non-Goals: no online translation.\n"
                  "6. Architecture: content script + background worker.\n"
                  "7. Frontend Work Needed: yes — popup + content script.\n"
                  "8. Backend Work Needed: yes — dictionary service.\n"
                  "9. Test Plan: unit + e2e via Playwright.\n"
                  "10. Open Questions: none.\n"
                  "STATUS: completed\n")
    # -- Argus review (codex) --------------------------------------------
    executor.push("codex",
                  "No blocking issues found.\nSTATUS: completed\n")
    # -- Athena present-for-approval (claude) ----------------------------
    executor.push("claude",
                  "Spec ready. Reply approve or revise.\nSTATUS: completed\n")
    # -- Athena decompose (claude) ---------------------------------------
    executor.push("claude",
                  '{"frontend": true, "backend": true, "test": true,'
                  '"frontend_brief": "Build content script + popup",'
                  '"backend_brief": "Build dictionary lookup service",'
                  '"test_brief": "Validate end-to-end double-click flow"}\n'
                  "STATUS: completed\n")
    # -- Apollo specialist (gemini) --------------------------------------
    executor.push("gemini",
                  "Apollo here. Plan: build popup.html, popup.js, content.js.\n"
                  "STATUS: in_progress\n")
    # -- Atlas specialist (claude) ---------------------------------------
    executor.push("claude",
                  "Atlas here. Plan: dictionary loader, query API, error handling.\n"
                  "STATUS: in_progress\n")
    # -- Hephaestus specialist (codex) -----------------------------------
    executor.push("codex",
                  "Hephaestus here. Plan: write Playwright tests for selection flow.\n"
                  "STATUS: in_progress\n")
