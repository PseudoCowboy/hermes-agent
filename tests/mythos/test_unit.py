"""Unit tests for state, intake, and runner helpers.

These don't touch the orchestrator — they keep the small modules honest
on their own so a regression in one component doesn't only show up in
the giant integration test.
"""

from pathlib import Path

import pytest

from mythos.intake import IntakeClassifier, is_approval, is_revision_request
from mythos.runners import (
    AgentRequest,
    AgentResult,
    ScriptedRunner,
    SubprocessRunner,
    _chunk_for_discord,
    shell_preview,
)
from mythos.roles import AgentRole, ROLE_REGISTRY, role_for_name, CliKind
from mythos.state import (
    ProjectPhase,
    ProjectRecord,
    ProjectStore,
    ReviewRound,
    WorkstreamKind,
    WorkstreamRecord,
    WorkstreamStatus,
    new_project_id,
)


# ── intake classifier ────────────────────────────────────────────────────

def test_intake_classifier_accepts_project_request():
    cls = IntakeClassifier(["build", "extension", "translator"])
    decision = cls.classify("I want to build a chrome translator extension please")
    assert decision.is_project, decision.reason


def test_intake_classifier_rejects_short_or_meta():
    cls = IntakeClassifier(["build"])
    assert not cls.classify("").is_project
    assert not cls.classify("hi").is_project
    assert not cls.classify("?help").is_project
    assert not cls.classify("!status").is_project


def test_intake_classifier_rejects_no_keyword_match():
    cls = IntakeClassifier(["build"])
    decision = cls.classify("how is everyone doing today on this fine morning")
    assert not decision.is_project


def test_is_approval_matches_common_phrasings():
    assert is_approval("approve")
    assert is_approval("LGTM!")
    assert is_approval("approved")
    assert is_approval("looks good")
    assert is_approval("👍")
    assert not is_approval("change the popup color")
    assert not is_approval("")


def test_is_revision_request_matches_diff_phrasings():
    assert is_revision_request("change the popup colour to blue")
    assert is_revision_request("redo the storage layer")
    assert not is_revision_request("approve")


# ── role registry ────────────────────────────────────────────────────────

def test_role_registry_has_required_assignments():
    assert ROLE_REGISTRY[AgentRole.MAIN].cli is CliKind.CLAUDE_CODE
    assert ROLE_REGISTRY[AgentRole.MAIN].name == "Athena"
    assert ROLE_REGISTRY[AgentRole.DRAFT_PLAN].cli is CliKind.CLAUDE_CODE
    assert ROLE_REGISTRY[AgentRole.DRAFT_PLAN].name == "Prometheus"
    assert ROLE_REGISTRY[AgentRole.REVIEW].cli is CliKind.CODEX
    assert ROLE_REGISTRY[AgentRole.REVIEW].name == "Argus"
    assert ROLE_REGISTRY[AgentRole.TEST].cli is CliKind.CODEX
    assert ROLE_REGISTRY[AgentRole.TEST].name == "Hephaestus"
    assert ROLE_REGISTRY[AgentRole.FRONTEND].cli is CliKind.GEMINI
    assert ROLE_REGISTRY[AgentRole.FRONTEND].name == "Apollo"
    assert ROLE_REGISTRY[AgentRole.BACKEND].cli is CliKind.CLAUDE_CODE
    assert ROLE_REGISTRY[AgentRole.BACKEND].name == "Atlas"


def test_role_for_name_reverse_lookup():
    assert role_for_name("Athena") is AgentRole.MAIN
    assert role_for_name("argus") is AgentRole.REVIEW
    assert role_for_name("nobody") is None


def test_claude_code_argv_contains_required_flags():
    d = ROLE_REGISTRY[AgentRole.MAIN]
    assert d.argv[0] == "claude"
    assert "--dangerously-skip-permissions" in d.argv
    assert "--effort" in d.argv and "high" in d.argv
    assert "--print" in d.argv
    assert d.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4141"
    assert d.env["ANTHROPIC_AUTH_TOKEN"] == "dummy"
    assert d.env["ANTHROPIC_MODEL"] == "claude-opus-4.7-1m-internal"


def test_codex_argv_contains_required_flags():
    d = ROLE_REGISTRY[AgentRole.REVIEW]
    assert d.argv[:2] == ["codex", "exec"]
    assert "-m" in d.argv and "gpt-5.5" in d.argv
    assert "model_reasoning_effort=high" in d.argv
    assert "--skip-git-repo-check" in d.argv
    assert d.env["OPENAI_BASE_URL"] == "http://127.0.0.1:4141/v1"
    assert d.env["OPENAI_API_KEY"] == "dummy"


def test_gemini_argv_contains_required_flags():
    d = ROLE_REGISTRY[AgentRole.FRONTEND]
    assert d.argv[0] == "gemini"
    assert "-m" in d.argv and "gemini-3.1-pro-preview" in d.argv
    assert "-y" in d.argv
    # Gemini uses host credentials — no ANTHROPIC/OPENAI env should be present.
    assert "ANTHROPIC_BASE_URL" not in d.env
    assert "OPENAI_API_KEY" not in d.env


# ── runners ──────────────────────────────────────────────────────────────

def test_chunk_for_discord_splits_long_text():
    chunks = _chunk_for_discord("a" * 5000, limit=1900)
    assert len(chunks) >= 3
    assert all(len(c) <= 1900 for c in chunks)


def test_chunk_for_discord_prefers_newline_break():
    text = ("foo\n" * 600).rstrip()
    chunks = _chunk_for_discord(text, limit=1900)
    for c in chunks[:-1]:
        # break on newline (last char of a chunk should be content, not a partial line)
        assert "\n" in c


def test_scripted_runner_returns_scripted_outputs(tmp_path):
    runner = ScriptedRunner({AgentRole.MAIN: ["hello world"]})
    res = runner.run(
        AgentRequest(role=AgentRole.MAIN, prompt="hi", workspace=tmp_path),
        ROLE_REGISTRY[AgentRole.MAIN],
    )
    assert res.stdout == "hello world"
    assert res.exit_code == 0
    assert res.messages == ["hello world"]


def test_subprocess_runner_invokes_correct_command(tmp_path):
    captured = {}

    class FakeProc:
        returncode = 0
        def communicate(self, input=None, timeout=None):
            captured["stdin"] = input
            return ("ok", "")

    def fake_popen(argv, cwd, env, stdin, stdout, stderr, text):
        captured["argv"] = list(argv)
        captured["cwd"] = cwd
        captured["env"] = env
        return FakeProc()

    runner = SubprocessRunner(popen=fake_popen)
    definition = ROLE_REGISTRY[AgentRole.MAIN]
    res = runner.run(
        AgentRequest(role=AgentRole.MAIN, prompt="draft something", workspace=tmp_path),
        definition,
    )
    assert res.exit_code == 0
    assert captured["argv"][0] == "claude"
    assert "--print" in captured["argv"]
    # Prompt routed via stdin for claude.
    assert "draft something" in captured["stdin"]
    # Env carries the configured Anthropic vars.
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4141"
    assert captured["env"]["ANTHROPIC_MODEL"] == "claude-opus-4.7-1m-internal"


def test_subprocess_runner_routes_gemini_prompt_to_argv(tmp_path):
    captured = {}

    class FakeProc:
        returncode = 0
        def communicate(self, input=None, timeout=None):
            captured["stdin"] = input
            return ("frontend ok", "")

    def fake_popen(argv, cwd, env, stdin, stdout, stderr, text):
        captured["argv"] = list(argv)
        return FakeProc()

    runner = SubprocessRunner(popen=fake_popen)
    runner.run(
        AgentRequest(role=AgentRole.FRONTEND, prompt="implement popup", workspace=tmp_path),
        ROLE_REGISTRY[AgentRole.FRONTEND],
    )
    assert "-p" in captured["argv"]
    p_idx = captured["argv"].index("-p")
    assert "implement popup" in captured["argv"][p_idx + 1]
    assert captured["stdin"] == ""


def test_subprocess_runner_handles_missing_binary(tmp_path):
    def fake_popen(*a, **kw):
        raise FileNotFoundError("no such file")
    runner = SubprocessRunner(popen=fake_popen)
    res = runner.run(
        AgentRequest(role=AgentRole.MAIN, prompt="x", workspace=tmp_path),
        ROLE_REGISTRY[AgentRole.MAIN],
    )
    assert res.exit_code == 127
    assert "CLI not found" in res.error


def test_shell_preview_quotes_args():
    preview = shell_preview(ROLE_REGISTRY[AgentRole.FRONTEND], "this needs quoting")
    assert "gemini" in preview
    assert "'this needs quoting'" in preview or "\"this needs quoting\"" in preview


# ── state store ──────────────────────────────────────────────────────────

def test_project_store_create_and_get(tmp_path):
    store = ProjectStore(tmp_path)
    record = ProjectRecord(
        project_id=new_project_id(),
        source_message_id="m1",
        owner_user_id="u1",
        request_text="build x",
        main_channel_id="c1",
    )
    store.create(record)
    loaded = store.get(record.project_id)
    assert loaded is not None
    assert loaded.request_text == "build x"


def test_project_store_edit_round_trips_workstreams(tmp_path):
    store = ProjectStore(tmp_path)
    record = ProjectRecord(
        project_id=new_project_id(),
        source_message_id="m1",
        owner_user_id="u1",
        request_text="r",
        main_channel_id="c1",
    )
    store.create(record)
    with store.edit(record.project_id) as rec:
        rec.workstreams[WorkstreamKind.FRONTEND.value] = WorkstreamRecord(
            kind=WorkstreamKind.FRONTEND,
            channel_id="ch_99",
            channel_name="proj-frontend",
            status=WorkstreamStatus.RUNNING,
        )
        rec.review_rounds.append(ReviewRound(round_number=1, design_version=1, accepted=True))
        rec.phase = ProjectPhase.IMPLEMENTING
    reloaded = store.get(record.project_id)
    assert reloaded.phase is ProjectPhase.IMPLEMENTING
    assert reloaded.workstreams[WorkstreamKind.FRONTEND.value].status is WorkstreamStatus.RUNNING
    assert reloaded.review_rounds[0].accepted


def test_project_store_idempotency_claims_once(tmp_path):
    store = ProjectStore(tmp_path)
    assert store.claim_idempotency("k1")
    assert not store.claim_idempotency("k1")
    assert store.lookup_idempotency("k1") is not None


def test_project_store_create_rejects_duplicate(tmp_path):
    store = ProjectStore(tmp_path)
    record = ProjectRecord(
        project_id="abc",
        source_message_id="m1",
        owner_user_id="u1",
        request_text="r",
        main_channel_id="c1",
    )
    store.create(record)
    with pytest.raises(FileExistsError):
        store.create(record)
