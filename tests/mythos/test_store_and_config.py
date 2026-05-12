"""Unit tests for store and config — no Discord, no async."""
import sqlite3
from pathlib import Path

import pytest

from mythos.config import MythosConfig, ROLE_BOT_TOKEN_ENV_VARS
from mythos.models import (
    ApprovalEvent,
    Project,
    ProjectState,
    Review,
    SpecVersion,
)
from mythos.store import Store


def _clear_role_bot_env(monkeypatch):
    for env_names in ROLE_BOT_TOKEN_ENV_VARS.values():
        for env_name in env_names:
            monkeypatch.delenv(env_name, raising=False)


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
    p.completed_disciplines = ["frontend"]
    s.update_project(p)
    got = s.get_project("abc123")
    assert got.state == ProjectState.DRAFTING
    assert got.discipline_channels == {"frontend": 999}
    assert got.completed_disciplines == ["frontend"]

    s.insert_spec(SpecVersion(project_id="abc123", version=1, content="spec1"))
    s.insert_spec(SpecVersion(project_id="abc123", version=2, content="spec2"))
    latest = s.latest_spec("abc123")
    assert latest.version == 2

    s.insert_review(Review(project_id="abc123", spec_version=2, iteration=1, content="ok"))
    assert s.latest_review("abc123").content == "ok"

    s.record_approval(ApprovalEvent(project_id="abc123", spec_version=2, user_id=42))
    assert s.get_approval("abc123").user_id == 42


def test_store_migrates_completed_disciplines_column(tmp_state_dir):
    db_path = Path(tmp_state_dir) / "legacy.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE projects (
            project_id TEXT PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            seed_request TEXT NOT NULL,
            project_channel_id INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL,
            created_at REAL NOT NULL,
            spec_version INTEGER NOT NULL DEFAULT 0,
            review_iteration INTEGER NOT NULL DEFAULT 0,
            discipline_channels TEXT NOT NULL DEFAULT '{}',
            working_dir TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.execute(
        """INSERT INTO projects (
            project_id, owner_user_id, seed_request, project_channel_id,
            state, created_at, spec_version, review_iteration,
            discipline_channels, working_dir
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("legacy", 7, "old", 123, ProjectState.IN_PROGRESS.value, 1.0, 1, 1, "{}", "/tmp/x"),
    )
    conn.commit()
    conn.close()

    s = Store(db_path)
    got = s.get_project("legacy")
    assert got is not None
    assert got.completed_disciplines == []


def test_get_project_by_channel(tmp_state_dir):
    s = Store(Path(tmp_state_dir) / "db.sqlite")
    p = Project(project_id="x", owner_user_id=1, seed_request="r", project_channel_id=555)
    s.insert_project(p)
    got = s.get_project_by_channel(555)
    assert got is not None and got.project_id == "x"
    assert s.get_project_by_channel(123) is None


def test_config_defaults_and_env_overrides(monkeypatch, tmp_state_dir):
    _clear_role_bot_env(monkeypatch)
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
    assert "--dangerously-bypass-approvals-and-sandbox" in argus.command
    # Model override applied to -m position.
    assert "gpt-test" in argus.command

    apollo = cfg.agent_cli["apollo"]
    assert apollo.kind == "gemini"
    assert apollo.prompt_via.startswith("flag:")
    assert "gemini-test" in apollo.command


def test_config_loads_role_bot_tokens_from_env(monkeypatch):
    _clear_role_bot_env(monkeypatch)
    monkeypatch.setenv("DISCORD_PROMETHEUS_BOT_TOKEN", "prom-env")
    monkeypatch.setenv("MYTHOS_ARGUS_BOT_TOKEN", "argus-env")
    monkeypatch.setenv("DISCORD_APOLLO_BOT_TOKEN", "apollo-env")

    cfg = MythosConfig.from_env_and_yaml(None)

    assert cfg.discord_role_bot_tokens["prometheus"] == "prom-env"
    assert cfg.discord_role_bot_tokens["argus"] == "argus-env"
    assert cfg.discord_role_bot_tokens["apollo"] == "apollo-env"
    assert "atlas" not in cfg.discord_role_bot_tokens


def test_yaml_overrides(monkeypatch, tmp_state_dir):
    _clear_role_bot_env(monkeypatch)
    yaml_path = Path(tmp_state_dir) / "c.yaml"
    yaml_path.write_text(
        "max_review_iterations: 5\n"
        "discord_role_bots:\n"
        "  prometheus: yaml-prom\n"
        "  atlas: yaml-atlas\n"
        "  unknown: ignored\n"
        "agents:\n"
        "  apollo:\n"
        "    timeout_seconds: 1234\n"
        "    env:\n"
        "      GEMINI_API_KEY: yaml-key\n"
    )
    cfg = MythosConfig.from_env_and_yaml(yaml_path)
    assert cfg.max_review_iterations == 5
    assert cfg.discord_role_bot_tokens == {
        "prometheus": "yaml-prom",
        "atlas": "yaml-atlas",
    }
    assert cfg.agent_cli["apollo"].timeout_seconds == 1234
    assert cfg.agent_cli["apollo"].env["GEMINI_API_KEY"] == "yaml-key"


def test_env_role_bot_tokens_override_yaml(monkeypatch, tmp_state_dir):
    _clear_role_bot_env(monkeypatch)
    yaml_path = Path(tmp_state_dir) / "c.yaml"
    yaml_path.write_text(
        "discord_role_bots:\n"
        "  prometheus: yaml-prom\n"
        "  atlas: yaml-atlas\n"
    )
    monkeypatch.setenv("MYTHOS_PROMETHEUS_BOT_TOKEN", "env-prom")

    cfg = MythosConfig.from_env_and_yaml(yaml_path)

    assert cfg.discord_role_bot_tokens["prometheus"] == "env-prom"
    assert cfg.discord_role_bot_tokens["atlas"] == "yaml-atlas"
