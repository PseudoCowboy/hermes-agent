"""CliAgent subprocess test using a fake CLI script.

Avoids requiring claude/codex/gemini installed by writing a tiny shell
script that emulates one CLI's contract (echo stdin OR last arg, exit
with the env-supplied code, etc.) and pointing CliAgent at it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from mythos.agents import AgentError, AgentInput, CliAgent
from mythos.roles import Backend, Role, RoleBinding


def _write_fake_cli(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "fakecli"
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


@pytest.mark.asyncio
async def test_cli_agent_pipes_prompt_to_stdin(tmp_path: Path):
    """Claude/Codex style: prompt arrives on stdin."""
    cli = _write_fake_cli(
        tmp_path,
        # Echo the env var, then the stdin prompt.
        'echo "MARKER=$MARKER"\ncat\n',
    )
    binding = RoleBinding(
        role=Role.PROMETHEUS,
        backend=Backend.CLAUDE_CODE,
        command=[str(cli)],
        env={"MARKER": "from-binding"},
        prompt_as_arg=False,
    )
    agent = CliAgent(binding)
    out = await agent.invoke(
        AgentInput(prompt="hello world", workdir=tmp_path)
    )
    assert out.ok
    assert "MARKER=from-binding" in out.text
    assert "hello world" in out.text


@pytest.mark.asyncio
async def test_cli_agent_appends_prompt_as_arg(tmp_path: Path):
    """Gemini style: prompt is the last positional arg."""
    cli = _write_fake_cli(
        tmp_path,
        # Echo the last arg.
        'echo "PROMPT_ARG=${@: -1}"\n',
    )
    binding = RoleBinding(
        role=Role.APOLLO,
        backend=Backend.GEMINI,
        command=[str(cli), "-y", "-p"],
        env={},
        prompt_as_arg=True,
    )
    agent = CliAgent(binding)
    out = await agent.invoke(
        AgentInput(prompt="implement-the-thing", workdir=tmp_path)
    )
    assert out.ok
    assert "PROMPT_ARG=implement-the-thing" in out.text


@pytest.mark.asyncio
async def test_cli_agent_runs_in_workdir(tmp_path: Path):
    cli = _write_fake_cli(tmp_path, 'pwd\n')
    workdir = tmp_path / "subdir"
    workdir.mkdir()
    binding = RoleBinding(
        role=Role.ATLAS,
        backend=Backend.CLAUDE_CODE,
        command=[str(cli)],
        env={},
        prompt_as_arg=False,
    )
    agent = CliAgent(binding)
    out = await agent.invoke(AgentInput(prompt="", workdir=workdir))
    assert out.ok
    assert str(workdir.resolve()) in out.text


@pytest.mark.asyncio
async def test_cli_agent_nonzero_exit_surfaces_in_output(tmp_path: Path):
    cli = _write_fake_cli(tmp_path, 'echo "boom" >&2\nexit 7\n')
    binding = RoleBinding(
        role=Role.HEPHAESTUS,
        backend=Backend.CODEX,
        command=[str(cli)],
        env={},
        prompt_as_arg=False,
    )
    agent = CliAgent(binding)
    out = await agent.invoke(AgentInput(prompt="x", workdir=tmp_path))
    assert not out.ok
    assert out.exit_code == 7
    assert "boom" in out.stderr


@pytest.mark.asyncio
async def test_cli_agent_missing_binary_raises(tmp_path: Path):
    binding = RoleBinding(
        role=Role.ARGUS,
        backend=Backend.CODEX,
        command=["/does/not/exist/never/ever"],
        env={},
        prompt_as_arg=False,
    )
    agent = CliAgent(binding)
    with pytest.raises(AgentError):
        await agent.invoke(AgentInput(prompt="x", workdir=tmp_path))
