"""Tests for agent runners — both the FakeRunner (used in tests) and the
subprocess runners (verified against a tiny shell stub instead of real CLIs).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from mythos.runners import (
    AgentPromptPacket,
    ClaudeCodeRunner,
    CodexRunner,
    FakeRunner,
    GeminiRunner,
)


def _packet(tmp_path: Path, role: str = "main") -> AgentPromptPacket:
    return AgentPromptPacket(
        project_id="p1", project_title="t", role=role, phase="planning",
        target_channel_id="c", workspace_path=str(tmp_path),
        allowed_questions_channel_id="c",
        request_text="build something",
    )


def test_fake_runner_returns_registered_response(tmp_path):
    runner = FakeRunner()
    runner.register("main", lambda pk: f"hello from {pk.role}")
    res = runner.run(
        packet=_packet(tmp_path, "main"),
        command=["fake"], env={}, log_dir=tmp_path,
    )
    assert res.ok
    assert "hello from main" in res.stdout
    assert runner.calls and runner.calls[0].role == "main"


def test_fake_runner_handles_unregistered_role(tmp_path):
    runner = FakeRunner()
    res = runner.run(
        packet=_packet(tmp_path, "review"),
        command=["fake"], env={}, log_dir=tmp_path,
    )
    assert res.ok  # default behavior: return placeholder, exit 0


def test_runner_prompt_includes_required_fields(tmp_path):
    pk = _packet(tmp_path, "draft_plan")
    pk.task_brief = "implement X"
    pk.task_id = "tsk_1"
    pk.approved_design_artifact = "## DESIGN"
    rendered = pk.render_prompt()
    assert "draft_plan" in rendered
    assert "## DESIGN" in rendered
    assert "tsk_1" in rendered
    assert "implement X" in rendered
    assert pk.allowed_questions_channel_id in rendered


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only stub")
def test_claude_runner_subprocess_with_stub(tmp_path, monkeypatch):
    """Use a shell stub instead of real `claude` to verify env + stdin wiring."""
    stub = tmp_path / "claude_stub.sh"
    stub.write_text(
        '#!/bin/sh\n'
        'cat\n'
        'echo "[env] $ANTHROPIC_BASE_URL/$ANTHROPIC_MODEL" >&2\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)

    runner = ClaudeCodeRunner()
    res = runner.run(
        packet=_packet(tmp_path, "main"),
        command=[str(stub)],
        env={
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:4141",
            "ANTHROPIC_MODEL": "claude-opus-4.7-1m-internal",
            "ANTHROPIC_AUTH_TOKEN": "dummy",
        },
        log_dir=tmp_path,
    )
    assert res.exit_code == 0
    # Stub echoed prompt to stdout
    assert "Mythos agent run" in res.stdout
    # Stub printed env to stderr — confirms env var was scoped correctly.
    assert "127.0.0.1:4141" in res.stderr
    assert "claude-opus-4.7-1m-internal" in res.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only stub")
def test_codex_runner_passes_prompt_as_arg(tmp_path):
    """Codex receives the prompt as the trailing positional arg."""
    stub = tmp_path / "codex_stub.sh"
    stub.write_text(
        '#!/bin/sh\n'
        'echo "ARGS:$#"\n'
        'echo "LAST:$@" | tail -c 200\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)

    runner = CodexRunner()
    res = runner.run(
        packet=_packet(tmp_path, "review"),
        command=[str(stub), "exec", "-m", "gpt-5.5"],
        env={"OPENAI_BASE_URL": "http://127.0.0.1:4141/v1"},
        log_dir=tmp_path,
    )
    assert res.exit_code == 0
    # 4 args = exec, -m, gpt-5.5, <prompt>
    assert "ARGS:4" in res.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only stub")
def test_gemini_runner_uses_dash_p(tmp_path):
    """Gemini receives the prompt via -p flag."""
    stub = tmp_path / "gemini_stub.sh"
    stub.write_text(
        '#!/bin/sh\n'
        'while [ $# -gt 0 ]; do\n'
        '  if [ "$1" = "-p" ]; then\n'
        '    shift; echo "PROMPT:$1" | head -c 80; echo\n'
        '  fi\n'
        '  shift || true\n'
        'done\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    runner = GeminiRunner()
    res = runner.run(
        packet=_packet(tmp_path, "frontend"),
        command=[str(stub), "-m", "gemini-3.1-pro-preview", "-y"],
        env={},
        log_dir=tmp_path,
    )
    assert res.exit_code == 0
    assert "PROMPT:" in res.stdout
