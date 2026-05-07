"""Defaults must match the build instructions exactly: claude/codex/gemini
commands, base URLs, and environment variables.
"""

from __future__ import annotations

from pathlib import Path

from mythos.agents import AgentBackend, AgentRole
from mythos.config import (
    DEFAULT_ANTHROPIC_BASE_URL,
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_CODEX_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OPENAI_BASE_URL,
    load_config,
)
from mythos.runners import SubprocessAgentRunner


def test_default_claude_code_env():
    cfg = load_config(path=Path("/nonexistent.yaml"))
    main = cfg.agent(AgentRole.MAIN)
    assert main.model == DEFAULT_ANTHROPIC_MODEL
    assert main.effort == "high"
    assert main.base_url == DEFAULT_ANTHROPIC_BASE_URL
    assert main.auth_env["ANTHROPIC_BASE_URL"] == DEFAULT_ANTHROPIC_BASE_URL
    assert main.auth_env["ANTHROPIC_AUTH_TOKEN"] == "dummy"
    assert main.auth_env["ANTHROPIC_MODEL"] == DEFAULT_ANTHROPIC_MODEL


def test_default_codex_env():
    cfg = load_config(path=Path("/nonexistent.yaml"))
    review = cfg.agent(AgentRole.REVIEW)
    assert review.model == DEFAULT_CODEX_MODEL
    assert review.effort == "high"
    assert review.base_url == DEFAULT_OPENAI_BASE_URL
    assert review.auth_env["OPENAI_BASE_URL"] == DEFAULT_OPENAI_BASE_URL
    assert review.auth_env["OPENAI_API_KEY"] == "dummy"


def test_default_gemini_no_proxy_env():
    cfg = load_config(path=Path("/nonexistent.yaml"))
    fe = cfg.agent(AgentRole.FRONTEND)
    assert fe.model == DEFAULT_GEMINI_MODEL
    assert fe.auth_env == {}  # uses host's preconfigured creds


def test_subprocess_command_shapes():
    """The exact CLI command shape must match the build instructions."""
    cfg = load_config(path=Path("/nonexistent.yaml"))
    runner = SubprocessAgentRunner()

    cmd, stdin, env = runner._build_invocation(
        AgentBackend.CLAUDE_CODE, "hello", cfg.agent(AgentRole.MAIN)
    )
    assert cmd[:5] == ["claude", "--dangerously-skip-permissions", "--effort", "high", "--print"]
    assert stdin == b"hello"
    assert env["ANTHROPIC_BASE_URL"] == DEFAULT_ANTHROPIC_BASE_URL

    cmd, stdin, env = runner._build_invocation(
        AgentBackend.CODEX, "review this", cfg.agent(AgentRole.REVIEW)
    )
    assert cmd[0:2] == ["codex", "exec"]
    assert "-m" in cmd and DEFAULT_CODEX_MODEL in cmd
    assert "model_reasoning_effort=high" in cmd
    assert "--skip-git-repo-check" in cmd
    assert cmd[-1] == "review this"
    assert stdin is None
    assert env["OPENAI_BASE_URL"] == DEFAULT_OPENAI_BASE_URL

    cmd, stdin, env = runner._build_invocation(
        AgentBackend.GEMINI, "build ui", cfg.agent(AgentRole.FRONTEND)
    )
    assert cmd[0] == "gemini"
    assert "-p" in cmd
    assert "build ui" in cmd
    assert "-m" in cmd and DEFAULT_GEMINI_MODEL in cmd
    assert "-y" in cmd


def test_yaml_overrides_apply(tmp_path):
    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text(
        """
discord:
  guild_id: 12345
  main_channel_id: 67890
max_revision_rounds: 4
agents:
  main:
    model: claude-custom
    timeout_seconds: 60
"""
    )
    cfg = load_config(path=yaml_path)
    assert cfg.discord_guild_id == 12345
    assert cfg.discord_main_channel_id == 67890
    assert cfg.max_revision_rounds == 4
    assert cfg.agent(AgentRole.MAIN).model == "claude-custom"
    assert cfg.agent(AgentRole.MAIN).timeout_seconds == 60
