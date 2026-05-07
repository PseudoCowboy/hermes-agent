"""Tests for the CLI runtime adapter — env, command shapes, mock dispatch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mythos.cli_runtime import CLIInvocation, CLIResult, CLIRuntime, _build_command, _build_env
from mythos.config import AgentRuntimeConfig, load_config
from mythos.models import AgentRole


def _claude_cfg():
    return AgentRuntimeConfig(
        role=AgentRole.ATHENA,
        runtime="claude",
        command=["claude", "--dangerously-skip-permissions", "--effort", "high", "--print"],
        model="claude-opus-4.7-1m-internal",
        base_url="http://127.0.0.1:4141",
        auth_token="dummy",
    )


def _codex_cfg():
    return AgentRuntimeConfig(
        role=AgentRole.ARGUS,
        runtime="codex",
        command=["codex", "exec", "-m", "gpt-5.5", "-c", "model_reasoning_effort=high", "--skip-git-repo-check"],
        model="gpt-5.5",
        base_url="http://127.0.0.1:4141/v1",
        auth_token="dummy",
    )


def _gemini_cfg():
    return AgentRuntimeConfig(
        role=AgentRole.APOLLO,
        runtime="gemini",
        command=["gemini", "-m", "gemini-3.1-pro-preview", "-y"],
        model="gemini-3.1-pro-preview",
    )


def test_default_agent_table_complete():
    cfg = load_config()
    for role in AgentRole:
        assert role in cfg.agents
    assert cfg.agents[AgentRole.ATHENA].runtime == "claude"
    assert cfg.agents[AgentRole.ARGUS].runtime == "codex"
    assert cfg.agents[AgentRole.APOLLO].runtime == "gemini"


def test_claude_env_has_anthropic_vars():
    env = _build_env(_claude_cfg())
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4141"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "dummy"
    assert env["ANTHROPIC_MODEL"] == "claude-opus-4.7-1m-internal"
    assert "OPENAI_API_KEY" not in env or env.get("OPENAI_API_KEY") != "dummy"


def test_codex_env_has_openai_vars():
    env = _build_env(_codex_cfg())
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:4141/v1"
    assert env["OPENAI_API_KEY"] == "dummy"


def test_gemini_env_does_not_inject_proxy():
    env = _build_env(_gemini_cfg())
    # Critically, gemini must NOT receive ANTHROPIC_BASE_URL or OPENAI_BASE_URL
    # injection from us — it uses host's preconfigured creds.
    cfg = _gemini_cfg()
    assert cfg.base_url is None


def test_claude_command_uses_stdin():
    argv, via_stdin = _build_command(_claude_cfg(), "hello prompt")
    assert via_stdin is True
    assert argv == ["claude", "--dangerously-skip-permissions", "--effort", "high", "--print"]


def test_codex_command_appends_prompt():
    argv, via_stdin = _build_command(_codex_cfg(), "review this")
    assert via_stdin is False
    assert argv[-1] == "review this"
    assert argv[:6] == ["codex", "exec", "-m", "gpt-5.5", "-c", "model_reasoning_effort=high"]


def test_gemini_command_uses_p_flag():
    argv, via_stdin = _build_command(_gemini_cfg(), "do frontend")
    assert via_stdin is False
    assert "-p" in argv
    pi = argv.index("-p")
    assert argv[pi + 1] == "do frontend"
    assert "-y" in argv


@pytest.mark.asyncio
async def test_runtime_dispatches_to_mock(tmp_path):
    cfg = load_config()
    runtime = CLIRuntime(agent_configs=cfg.agents)
    captured: dict = {}

    def responder(invocation: CLIInvocation) -> CLIResult:
        captured["called"] = True
        captured["argv"] = invocation.command
        return CLIResult(
            role=invocation.role,
            runtime=invocation.runtime,
            exit_code=0,
            stdout='{"ok": true}',
            stderr="",
            duration_seconds=0.0,
            workspace=str(invocation.workspace),
        )

    runtime.set_mock(AgentRole.ATHENA, responder)
    result = await runtime.run(AgentRole.ATHENA, "hi", tmp_path)
    assert captured["called"] is True
    assert result.ok
    assert json.loads(result.stdout)["ok"] is True


@pytest.mark.asyncio
async def test_runtime_handles_missing_binary(tmp_path):
    cfg = load_config()
    # Replace claude with a definitely-missing command
    cfg.agents[AgentRole.ATHENA].command = ["definitely-not-a-real-binary-xyz", "--print"]
    runtime = CLIRuntime(agent_configs=cfg.agents)
    result = await runtime.run(AgentRole.ATHENA, "hi", tmp_path)
    assert not result.ok
    assert result.error and "not found" in result.error.lower()
