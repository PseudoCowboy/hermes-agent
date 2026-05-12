"""Per-role CLI bindings: defaults match the spec, env overrides apply."""

from __future__ import annotations

import os

import pytest

from mythos.roles import (
    Backend,
    ROLE_BINDINGS,
    Role,
    apply_env_overrides,
)


def test_default_bindings_cover_every_role():
    for role in Role:
        assert role in ROLE_BINDINGS


def test_claude_code_agents_default_command_and_env():
    for role in (Role.HERMES, Role.PROMETHEUS, Role.ATLAS):
        b = ROLE_BINDINGS[role]
        assert b.backend == Backend.CLAUDE_CODE
        assert b.command[0] == "claude"
        assert "--dangerously-skip-permissions" in b.command
        assert "--effort" in b.command and "high" in b.command
        assert "--print" in b.command
        assert b.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4141"
        assert b.env["ANTHROPIC_AUTH_TOKEN"] == "dummy"
        assert b.env["ANTHROPIC_MODEL"] == "claude-opus-4.7-1m-internal"
        # Prompt is piped to stdin, not appended.
        assert b.prompt_as_arg is False


def test_codex_agents_default_command_and_env():
    for role in (Role.ARGUS, Role.HEPHAESTUS):
        b = ROLE_BINDINGS[role]
        assert b.backend == Backend.CODEX
        assert b.command[:2] == ["codex", "exec"]
        assert "-m" in b.command
        i = b.command.index("-m")
        assert b.command[i + 1] == "gpt-5.5"
        assert "model_reasoning_effort=high" in b.command
        assert "--skip-git-repo-check" in b.command
        assert b.env["OPENAI_BASE_URL"] == "http://127.0.0.1:4141/v1"
        assert b.env["OPENAI_API_KEY"] == "dummy"
        assert b.prompt_as_arg is False


def test_gemini_agent_default_command_and_no_proxy_env():
    b = ROLE_BINDINGS[Role.APOLLO]
    assert b.backend == Backend.GEMINI
    assert b.command[0] == "gemini"
    assert "-y" in b.command  # yolo / approval-mode auto
    assert "-p" in b.command and b.command[-1] == "-p"
    i = b.command.index("-m")
    assert b.command[i + 1] == "gemini-3.1-pro-preview"
    # Gemini uses its own preconfigured creds; no proxy overrides.
    assert b.env == {}
    # Prompt is appended as the last positional arg.
    assert b.prompt_as_arg is True


def test_env_override_claude_model(monkeypatch):
    monkeypatch.setenv("MYTHOS_CLAUDE_MODEL", "some-other-model")
    monkeypatch.setenv("MYTHOS_CLAUDE_BASE_URL", "http://other:9999")
    bindings = apply_env_overrides(dict(ROLE_BINDINGS))
    b = bindings[Role.HERMES]
    assert b.env["ANTHROPIC_MODEL"] == "some-other-model"
    assert b.env["ANTHROPIC_BASE_URL"] == "http://other:9999"


def test_env_override_codex_model_replaces_in_command(monkeypatch):
    monkeypatch.setenv("MYTHOS_CODEX_MODEL", "gpt-7")
    bindings = apply_env_overrides(dict(ROLE_BINDINGS))
    b = bindings[Role.ARGUS]
    i = b.command.index("-m")
    assert b.command[i + 1] == "gpt-7"


def test_env_override_gemini_model(monkeypatch):
    monkeypatch.setenv("MYTHOS_GEMINI_MODEL", "gemini-99")
    bindings = apply_env_overrides(dict(ROLE_BINDINGS))
    b = bindings[Role.APOLLO]
    i = b.command.index("-m")
    assert b.command[i + 1] == "gemini-99"
