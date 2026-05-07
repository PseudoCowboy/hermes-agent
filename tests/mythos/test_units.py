"""Lower-level unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from mythos.config import (
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    PROVIDER_GEMINI,
    ROLE_PROVIDERS,
    load_config,
)
from mythos.discord_client import chunk_message, slugify
from mythos.protocol import (
    DECISION_APPROVE,
    DECISION_REQUEST_CHANGES,
    parse_agent_output,
)


def test_slugify_handles_garbage():
    assert slugify("Hello World!") == "hello-world"
    assert slugify("   ") == "project"
    assert slugify("LongTitle " * 20).startswith("longtitle-longtitle")
    assert "_" not in slugify("under_score")


def test_chunk_message_under_limit():
    assert chunk_message("hi", 50) == ["hi"]


def test_chunk_message_splits_at_lines():
    body = "\n".join(["a" * 60] * 4)
    chunks = chunk_message(body, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == body


def test_parse_design_block():
    raw = (
        "preamble\n"
        "<<MYTHOS:DESIGN_BEGIN>>\n# spec\n- bullet\n<<MYTHOS:DESIGN_END>>\n"
        "<<MYTHOS:STATUS:finished>>\n"
    )
    reply = parse_agent_output(raw)
    assert reply.design_body and reply.design_body.startswith("# spec")
    assert reply.finished
    assert reply.decision is None


def test_parse_review_decision():
    raw = "looks good\n<<MYTHOS:DECISION:approve>>\n<<MYTHOS:STATUS:finished>>"
    reply = parse_agent_output(raw)
    assert reply.decision == DECISION_APPROVE
    assert reply.finished


def test_parse_question():
    raw = "<<MYTHOS:STATUS:question>>\nWhat language?\n<<MYTHOS:STATUS:finished>>"
    reply = parse_agent_output(raw)
    assert reply.asks_question


def test_parse_no_markers_defaults_to_finished():
    reply = parse_agent_output("just text")
    assert reply.finished
    assert reply.visible_text == "just text"


def test_role_provider_map_is_immutable_and_correct():
    assert ROLE_PROVIDERS == {
        "athena": PROVIDER_CLAUDE,
        "prometheus": PROVIDER_CLAUDE,
        "atlas": PROVIDER_CLAUDE,
        "argus": PROVIDER_CODEX,
        "hephaestus": PROVIDER_CODEX,
        "apollo": PROVIDER_GEMINI,
    }


def test_load_config_env_overrides(tmp_path: Path):
    env = {
        "DISCORD_BOT_TOKEN": "xyz",
        "DISCORD_GUILD_ID": "12345",
        "DISCORD_MAIN_CHANNEL_ID": "67890",
        "MYTHOS_CLAUDE_MODEL": "claude-other",
        "MYTHOS_CODEX_MODEL": "gpt-other",
        "MYTHOS_GEMINI_MODEL": "gemini-other",
    }
    cfg = load_config(env=env)
    assert cfg.discord.bot_token == "xyz"
    assert cfg.discord.guild_id == 12345
    assert cfg.discord.main_channel_id == 67890
    assert cfg.providers[PROVIDER_CLAUDE].env["ANTHROPIC_MODEL"] == "claude-other"
    assert "gpt-other" in cfg.providers[PROVIDER_CODEX].command
    assert "gemini-other" in cfg.providers[PROVIDER_GEMINI].command


def test_load_config_defaults_match_spec():
    cfg = load_config(env={})
    claude = cfg.providers[PROVIDER_CLAUDE]
    assert claude.command == [
        "claude",
        "--dangerously-skip-permissions",
        "--effort",
        "high",
        "--print",
    ]
    assert claude.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4141"
    assert claude.env["ANTHROPIC_AUTH_TOKEN"] == "dummy"
    assert claude.env["ANTHROPIC_MODEL"] == "claude-opus-4.7-1m-internal"
    codex = cfg.providers[PROVIDER_CODEX]
    assert codex.command[:2] == ["codex", "exec"]
    assert "-m" in codex.command and "gpt-5.5" in codex.command
    assert "model_reasoning_effort=high" in codex.command
    assert "--skip-git-repo-check" in codex.command
    assert codex.env["OPENAI_BASE_URL"] == "http://127.0.0.1:4141/v1"
    gemini = cfg.providers[PROVIDER_GEMINI]
    assert gemini.command == ["gemini", "-m", "gemini-3.1-pro-preview", "-y"]
    assert gemini.prompt_arg_flag == "-p"


def test_workspace_subdirs_are_created(tmp_path: Path):
    from mythos.workspace import WORKSPACE_SUBDIRS, WorkspaceManager

    wm = WorkspaceManager(tmp_path)
    root = wm.allocate("translator", "proj_abc")
    for sub in WORKSPACE_SUBDIRS:
        assert (root / sub).is_dir()
