"""Unit tests for store and config — no Discord, no async."""
from pathlib import Path

import pytest

from mythos.config import MythosConfig
from mythos.models import (
    ApprovalEvent,
    Project,
    ProjectState,
    Review,
    SpecVersion,
)
from mythos.store import Store


def test_store_roundtrip(tmp_state_dir):
    s = Store(Path(tmp_state_dir) / "db.sqlite")
    p = Project(
        project_id="abc123",
        owner_user_id=42,
        seed_request="hi",
        project_channel_id=1234,
        state=ProjectState.CLARIFYING,
        working_dir="/tmp/wd",
    )
    s.insert_project(p)
    got = s.get_project("abc123")
    assert got.owner_user_id == 42
    assert got.state == ProjectState.CLARIFYING

    p.state = ProjectState.DRAFTING
    p.discipline_channels = {"frontend": 999}
    s.update_project(p)
    got = s.get_project("abc123")
    assert got.state == ProjectState.DRAFTING
    assert got.discipline_channels == {"frontend": 999}

    s.insert_spec(SpecVersion(project_id="abc123", version=1, content="spec1"))
    s.insert_spec(SpecVersion(project_id="abc123", version=2, content="spec2"))
    latest = s.latest_spec("abc123")
    assert latest.version == 2

    s.insert_review(Review(project_id="abc123", spec_version=2, iteration=1, content="ok"))
    assert s.latest_review("abc123").content == "ok"

    s.record_approval(ApprovalEvent(project_id="abc123", spec_version=2, user_id=42))
    assert s.get_approval("abc123").user_id == 42


def test_get_project_by_channel(tmp_state_dir):
    s = Store(Path(tmp_state_dir) / "db.sqlite")
    p = Project(project_id="x", owner_user_id=1, seed_request="r", project_channel_id=555)
    s.insert_project(p)
    got = s.get_project_by_channel(555)
    assert got is not None and got.project_id == "x"
    assert s.get_project_by_channel(123) is None


def test_config_defaults_and_env_overrides(monkeypatch, tmp_state_dir):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("DISCORD_GUILD_ID", "111")
    monkeypatch.setenv("MYTHOS_MAIN_CHANNEL_ID", "222")
    monkeypatch.setenv("MYTHOS_CLAUDE_BASE_URL", "http://override:9000")
    monkeypatch.setenv("MYTHOS_CLAUDE_MODEL", "claude-test-model")
    monkeypatch.setenv("MYTHOS_CODEX_MODEL", "gpt-test")
    monkeypatch.setenv("MYTHOS_GEMINI_MODEL", "gemini-test")

    cfg = MythosConfig.from_env_and_yaml(None)
    assert cfg.discord_bot_token == "tok"
    assert cfg.discord_guild_id == 111
    assert cfg.main_channel_id == 222

    # Defaults wired correctly
    athena = cfg.agent_cli["athena"]
    assert athena.kind == "claude"
    assert "--dangerously-skip-permissions" in athena.command
    assert athena.env["ANTHROPIC_BASE_URL"] == "http://override:9000"
    assert athena.env["ANTHROPIC_MODEL"] == "claude-test-model"

    argus = cfg.agent_cli["argus"]
    assert argus.kind == "codex"
    assert "exec" in argus.command
    # Model override applied to -m position.
    assert "gpt-test" in argus.command

    apollo = cfg.agent_cli["apollo"]
    assert apollo.kind == "gemini"
    assert apollo.prompt_via.startswith("flag:")
    assert "gemini-test" in apollo.command


def test_yaml_overrides(tmp_state_dir):
    yaml_path = Path(tmp_state_dir) / "c.yaml"
    yaml_path.write_text(
        "max_review_iterations: 5\n"
        "agents:\n"
        "  apollo:\n"
        "    timeout_seconds: 1234\n"
        "    env:\n"
        "      GEMINI_API_KEY: yaml-key\n"
    )
    cfg = MythosConfig.from_env_and_yaml(yaml_path)
    assert cfg.max_review_iterations == 5
    assert cfg.agent_cli["apollo"].timeout_seconds == 1234
    assert cfg.agent_cli["apollo"].env["GEMINI_API_KEY"] == "yaml-key"
