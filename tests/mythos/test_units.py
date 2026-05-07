"""Unit tests for the smaller mythos building blocks."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from mythos.config import (
    DEFAULT_RUNNER_BINDING,
    DEFAULT_RUNNER_COMMANDS,
    load_config,
    to_dict,
)
from mythos.projects import (
    Project,
    ProjectStatus,
    ProjectStore,
    make_project_id,
    slugify,
)
from mythos.runners.base import RunnerError
from mythos.runners.fake import FakeRunner
from mythos.runners.subprocess_runner import SubprocessRunner


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_default_role_to_runner_binding():
    cfg = load_config(env={})
    # All six required mythological agents present.
    expected_names = {"athena", "prometheus", "argus", "hephaestus", "apollo", "atlas"}
    assert set(cfg.agents.keys()) == expected_names

    # Mappings match the brief.
    assert cfg.agents["athena"].runner == "claude_code"
    assert cfg.agents["prometheus"].runner == "claude_code"
    assert cfg.agents["atlas"].runner == "claude_code"
    assert cfg.agents["argus"].runner == "codex"
    assert cfg.agents["hephaestus"].runner == "codex"
    assert cfg.agents["apollo"].runner == "gemini"


def test_default_runner_commands_match_brief():
    cfg = load_config(env={})
    cc = cfg.runners["claude_code"]
    assert cc.command[0] == "claude"
    assert "--dangerously-skip-permissions" in cc.command
    assert cc.command[cc.command.index("--effort") + 1] == "high"
    assert "--print" in cc.command
    assert cc.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4141"
    assert cc.env["ANTHROPIC_AUTH_TOKEN"] == "dummy"
    assert cc.env["ANTHROPIC_MODEL"] == "claude-opus-4.7-1m-internal"

    cx = cfg.runners["codex"]
    assert cx.command[:3] == ["codex", "exec", "-m"]
    assert "gpt-5.5" in cx.command
    assert "--skip-git-repo-check" in cx.command
    assert any(arg.startswith("model_reasoning_effort=") for arg in cx.command)
    assert cx.env["OPENAI_BASE_URL"] == "http://127.0.0.1:4141/v1"
    assert cx.env["OPENAI_API_KEY"] == "dummy"

    gm = cfg.runners["gemini"]
    assert gm.command[0] == "gemini"
    assert "gemini-3.1-pro-preview" in gm.command
    assert "-y" in gm.command
    # Gemini uses host's preconfigured creds — no proxy env.
    assert gm.env == {}


def test_runner_binding_override_via_env():
    cfg = load_config(env={"BACKEND_RUNNER": "codex"})
    assert cfg.agents["atlas"].runner == "codex"


def test_role_to_runner_override_unknown_runner_is_ignored():
    cfg = load_config(env={"BACKEND_RUNNER": "made_up"})
    # No change; default kept.
    assert cfg.agents["atlas"].runner == "claude_code"


def test_anthropic_api_key_alias_populates_auth_token():
    cfg = load_config(env={"ANTHROPIC_API_KEY": "real-secret-do-not-log"})
    assert cfg.runners["claude_code"].env["ANTHROPIC_AUTH_TOKEN"] == "real-secret-do-not-log"


def test_to_dict_redacts_secrets_and_lists_env_keys_only():
    cfg = load_config(env={"DISCORD_BOT_TOKEN": "secret"})
    snap = to_dict(cfg)
    assert snap["discord_bot_token"] == "***"
    assert "ANTHROPIC_AUTH_TOKEN" in snap["runners"]["claude_code"]["env_keys"]
    # No raw value in the snapshot.
    assert "secret" not in repr(snap)


# ---------------------------------------------------------------------------
# Project ID + slug
# ---------------------------------------------------------------------------


def test_project_id_generator_format():
    pid = make_project_id()
    assert pid.startswith("proj_") and len(pid) == len("proj_") + 8


def test_two_ids_distinct_in_same_second():
    a = make_project_id()
    b = make_project_id()
    assert a != b


def test_slugify_strips_specials():
    assert slugify("Hello World!") == "hello-world"
    assert slugify("translator   for   us") == "translator-for-us"
    assert slugify("   ") == "project"


# ---------------------------------------------------------------------------
# ProjectStore persistence
# ---------------------------------------------------------------------------


def test_store_create_persist_and_reopen(tmp_path):
    store = ProjectStore(str(tmp_path / "state.json"), str(tmp_path / "ws"))
    p = store.create(name="My App", requester_user_id="user-1", intake_text="hello")
    p.channel_category_id = "cat-1"
    p.sub_channel_ids["planning"] = "ch-planning"
    store.update(p)

    # Reopen and check.
    s2 = ProjectStore(str(tmp_path / "state.json"), str(tmp_path / "ws"))
    p2 = s2.get(p.id)
    assert p2 is not None
    assert p2.name == "My App"
    assert s2.get_by_channel("ch-planning") is not None
    assert s2.get_by_channel("cat-1") is not None


def test_store_workspace_isolation(tmp_path):
    store = ProjectStore(str(tmp_path / "state.json"), str(tmp_path / "ws"))
    p1 = store.create(name="A", requester_user_id="u", intake_text="a")
    p2 = store.create(name="B", requester_user_id="u", intake_text="b")
    assert Path(p1.workspace_path) != Path(p2.workspace_path)
    assert Path(p1.workspace_path).is_dir()
    assert Path(p2.workspace_path).is_dir()


def test_store_archive(tmp_path):
    store = ProjectStore(str(tmp_path / "state.json"), str(tmp_path / "ws"))
    p = store.create(name="A", requester_user_id="u", intake_text="a")
    store.archive(p.id)
    assert store.get(p.id).status == ProjectStatus.ARCHIVED
    assert store.active_count() == 0


# ---------------------------------------------------------------------------
# SubprocessRunner: real process via /usr/bin/cat (echoes stdin) & /bin/sh
# ---------------------------------------------------------------------------


def test_subprocess_runner_passes_prompt_via_stdin(tmp_path):
    from mythos.config import RunnerSpec

    spec = RunnerSpec(name="echo-stdin", command=["/bin/cat"], env={"FOO": "bar"})
    runner = SubprocessRunner(spec)
    res = runner.run("hello world", cwd=str(tmp_path), timeout_seconds=5)
    assert res.ok
    assert res.stdout == "hello world"


def test_subprocess_runner_passes_prompt_via_argv(tmp_path):
    from mythos.config import RunnerSpec

    # Mimic gemini's "-p prompt" delivery using /usr/bin/printf.
    spec = RunnerSpec(
        name="argv-runner",
        command=["/usr/bin/printf", "%s"],
        prompt_via_argv=True,
        prompt_argv_flag="--",
    )
    runner = SubprocessRunner(spec)
    res = runner.run("payload", cwd=str(tmp_path), timeout_seconds=5)
    assert res.returncode == 0
    assert "payload" in res.stdout


def test_subprocess_runner_timeout_marks_timed_out(tmp_path):
    from mythos.config import RunnerSpec

    spec = RunnerSpec(name="slow", command=["/bin/sh", "-c", "sleep 5"])
    runner = SubprocessRunner(spec)
    res = runner.run("", cwd=str(tmp_path), timeout_seconds=1)
    assert not res.ok
    assert res.timed_out


def test_subprocess_runner_missing_binary_raises(tmp_path):
    from mythos.config import RunnerSpec

    spec = RunnerSpec(name="missing", command=["/no/such/bin/anywhere"])
    with pytest.raises(RunnerError):
        SubprocessRunner(spec).run("hi", cwd=str(tmp_path))


# ---------------------------------------------------------------------------
# FakeRunner
# ---------------------------------------------------------------------------


def test_fake_runner_consumes_responses_in_order(tmp_path):
    fake = FakeRunner("fake", responses=["one", "two"])
    r1 = fake.run("p1", cwd=str(tmp_path))
    r2 = fake.run("p2", cwd=str(tmp_path))
    assert r1.stdout == "one"
    assert r2.stdout == "two"
    assert len(fake.calls) == 2
    assert fake.calls[0].prompt == "p1"


def test_fake_runner_dict_keyed_by_role(tmp_path):
    fake = FakeRunner("fake", responses={"main": ["athena-says"], "review": ["argus-says"]})
    rmain = fake.run("p", cwd=str(tmp_path), metadata={"role": "main"})
    rrev = fake.run("p", cwd=str(tmp_path), metadata={"role": "review"})
    assert rmain.stdout == "athena-says"
    assert rrev.stdout == "argus-says"


def test_fake_runner_callable(tmp_path):
    fake = FakeRunner("fake", responses=lambda prompt, cwd, meta: f"echo:{prompt}")
    assert fake.run("hi", cwd=str(tmp_path)).stdout == "echo:hi"
