"""Unit tests for mythos.config: defaults + env-var overrides."""

import os
from unittest import mock

from mythos.config import (
    DEFAULT_CLAUDE_CODE_COMMAND,
    DEFAULT_CLAUDE_CODE_ENV,
    DEFAULT_CODEX_COMMAND,
    DEFAULT_CODEX_ENV,
    DEFAULT_GEMINI_COMMAND,
    load_config,
)


def test_defaults_match_benchmark_spec():
    cfg = load_config()
    # Athena: Claude Code
    main = cfg.agent("main")
    assert main.codename == "Athena"
    assert main.provider == "claude"
    assert main.command == DEFAULT_CLAUDE_CODE_COMMAND
    for k, v in DEFAULT_CLAUDE_CODE_ENV.items():
        assert main.env[k] == v

    # Prometheus: Claude Code
    drafter = cfg.agent("draft_plan")
    assert drafter.codename == "Prometheus"
    assert drafter.provider == "claude"
    assert drafter.command == DEFAULT_CLAUDE_CODE_COMMAND

    # Atlas: Claude Code
    backend = cfg.agent("backend")
    assert backend.codename == "Atlas"
    assert backend.provider == "claude"

    # Argus: Codex
    review = cfg.agent("review")
    assert review.codename == "Argus"
    assert review.provider == "codex"
    assert review.command == DEFAULT_CODEX_COMMAND
    for k, v in DEFAULT_CODEX_ENV.items():
        assert review.env[k] == v

    # Hephaestus: Codex
    tester = cfg.agent("test")
    assert tester.codename == "Hephaestus"
    assert tester.provider == "codex"

    # Apollo: Gemini
    fe = cfg.agent("frontend")
    assert fe.codename == "Apollo"
    assert fe.provider == "gemini"
    assert fe.command == DEFAULT_GEMINI_COMMAND


def test_env_overrides_for_discord_settings():
    with mock.patch.dict(os.environ, {
        "DISCORD_BOT_TOKEN": "abc",
        "DISCORD_GUILD_ID": "11",
        "MYTHOS_MAIN_CHANNEL_ID": "22",
    }, clear=False):
        cfg = load_config()
    assert cfg.discord.bot_token == "abc"
    assert cfg.discord.guild_id == "11"
    assert cfg.discord.main_channel_id == "22"


def test_per_role_model_override_via_env():
    with mock.patch.dict(os.environ, {
        "MYTHOS_REVIEW_MODEL": "gpt-5.5-mini",
        "MYTHOS_BACKEND_MODEL": "claude-haiku-4-5",
    }, clear=False):
        cfg = load_config()
    review_cmd = cfg.agent("review").command
    assert "gpt-5.5-mini" in review_cmd
    backend_cmd = cfg.agent("backend").command
    # claude command has no -m flag by default; the override appends one
    assert "claude-haiku-4-5" in backend_cmd
