"""Unit tests for the AgentRunner subprocess invocation contract."""
from __future__ import annotations

from pathlib import Path

import pytest

from mythos.agent_runner import (AgentRunner, RunRequest, build_command,
                                   build_env, needs_stdin_prompt,
                                   parse_question, parse_status_line)
from mythos.config import (MythosConfig, default_claude_cli, default_codex_cli,
                            default_gemini_cli)
from mythos.roles import ChannelKind, Role


pytestmark_asyncio = pytest.mark.asyncio


def test_build_command_claude_uses_stdin():
    spec = default_claude_cli()
    argv = build_command(spec, "hello world")
    assert argv == [
        "claude", "--dangerously-skip-permissions",
        "--effort", "high", "--print",
    ]
    assert needs_stdin_prompt(spec)


def test_build_command_codex_uses_stdin():
    spec = default_codex_cli()
    argv = build_command(spec, "x")
    assert argv[0] == "codex"
    assert "exec" in argv and "--skip-git-repo-check" in argv
    assert "model_reasoning_effort=high" in " ".join(argv)
    assert needs_stdin_prompt(spec)


def test_build_command_gemini_appends_p_flag():
    spec = default_gemini_cli()
    argv = build_command(spec, "hello")
    assert argv == ["gemini", "-m", "gemini-3.1-pro-preview", "-y", "-p", "hello"]
    assert not needs_stdin_prompt(spec)


def test_build_env_includes_proxy_for_claude():
    env = build_env(default_claude_cli())
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4141"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "dummy"
    assert env["ANTHROPIC_MODEL"] == "claude-opus-4.7-1m-internal"


def test_build_env_includes_proxy_for_codex():
    env = build_env(default_codex_cli())
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:4141/v1"
    assert env["OPENAI_API_KEY"] == "dummy"


def test_parse_status_line_picks_last():
    assert parse_status_line("blah\nSTATUS: in_progress\nmore\nSTATUS: completed") == "completed"
    assert parse_status_line("no marker") == "completed"


def test_parse_question():
    assert parse_question("hi\nQUESTION: what color?\n") == "what color?"
    assert parse_question("nothing here") is None


@pytest.mark.asyncio
async def test_runner_executes_and_logs(tmp_path: Path):
    cfg = MythosConfig(workspace_root=tmp_path / "ws", state_path=tmp_path / "s.json",
                        agent_timeout_seconds=2)
    captured = {}

    async def fake_exec(argv, stdin_text, env, cwd, timeout):
        captured["argv"] = argv
        captured["stdin_text"] = stdin_text
        captured["env_keys"] = sorted(env.keys())
        return 0, "hello\nSTATUS: completed\n", ""

    runner = AgentRunner(cfg, executor=fake_exec)
    req = RunRequest(
        project_id="p1", run_id="r1", role=Role.PROMETHEUS,
        channel_kind=ChannelKind.PLAN, prompt="What up",
        workspace_dir=tmp_path / "ws",
        log_dir=tmp_path / "logs",
    )
    (tmp_path / "ws").mkdir()
    result = await runner.run(req)
    assert result.status == "completed"
    assert "hello" in result.summary
    assert result.log_path is not None and result.log_path.exists()
    log_text = result.log_path.read_text()
    assert "What up" in log_text
    assert "STATUS: completed" in log_text


@pytest.mark.asyncio
async def test_runner_failure_returns_failed(tmp_path: Path):
    cfg = MythosConfig(workspace_root=tmp_path / "ws", state_path=tmp_path / "s.json",
                        agent_timeout_seconds=2)

    async def fake_exec(argv, stdin_text, env, cwd, timeout):
        return 1, "", "boom: something broke"

    runner = AgentRunner(cfg, executor=fake_exec)
    (tmp_path / "ws").mkdir()
    req = RunRequest(
        project_id="p", run_id="r", role=Role.ARGUS,
        channel_kind=ChannelKind.PLAN, prompt="x",
        workspace_dir=tmp_path / "ws", log_dir=tmp_path / "logs",
    )
    result = await runner.run(req)
    assert result.status == "failed"
    assert "boom" in result.summary
    assert result.return_code == 1
