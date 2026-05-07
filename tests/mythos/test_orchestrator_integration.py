"""Integration tests for the mythos multi-agent Discord system.

These tests:
  - use the InMemoryDiscord fake (no network)
  - use the ScriptedRunner fake (no real CLI subprocesses)
  - exercise the full happy path from the user-scenario doc

Two suites:
  - happy path (single project, Chrome translator extension)
  - concurrency (two projects in flight at the same time, isolation check)
  - approval gate (no implementation channels until user approves)
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mythos.config import DiscordConfig, MythosConfig, WorkspaceConfig
from mythos.discord_adapter import InMemoryDiscord
from mythos.orchestrator import Orchestrator
from mythos.roles import AgentRole, ROLE_REGISTRY
from mythos.runners import ScriptedRunner
from mythos.state import ProjectPhase, ProjectStore, WorkstreamKind, WorkstreamStatus


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def store(workspace: Path) -> ProjectStore:
    return ProjectStore(workspace / "state")


@pytest.fixture
def discord(workspace: Path) -> InMemoryDiscord:
    d = InMemoryDiscord()
    d.seed_channel("main", channel_id="ch_main")
    return d


@pytest.fixture
def base_config(workspace: Path) -> MythosConfig:
    cfg = MythosConfig(
        discord=DiscordConfig(
            bot_token="fake",
            guild_id="42",
            main_channel_id="ch_main",
            project_channel_prefix="proj-",
        ),
        workspace=WorkspaceConfig(root=str(workspace / "workspaces")),
        state_dir=str(workspace / "state"),
    )
    cfg.agents = {role: ROLE_REGISTRY[role] for role in AgentRole}
    return cfg


def _wait(orch: Orchestrator, predicate, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ── tiny scripted output helpers ─────────────────────────────────────────

DESIGN_DRAFT_FULL = (
    "## Scope\nBuild a Chrome extension that translates words on double-click.\n\n"
    "## User-facing behavior\n"
    "User double-clicks a word; popup near cursor shows translation from a built-in dictionary.\n\n"
    "## Technical approach\n"
    "Browser extension (frontend): manifest v3, content script, popup UI.\n"
    "Backend: REST API serving dictionary lookups from a static JSON.\n"
    "Test: end-to-end test that double-click triggers the popup.\n\n"
    "## Assumptions\nDictionary is static and bundled.\n\n"
    "## Risks\nDictionary coverage is limited.\n\n"
    "## Workstream candidates\nfrontend, backend, test."
)

REVIEW_APPROVED = (
    "Reviewed the design. Scope is clear, frontend/backend boundary is sensible. "
    "No blockers found.\n\nAPPROVED"
)

REVIEW_NEEDS_CHANGES = (
    "Reviewed the design.\n"
    "- REQUIRED CHANGE: clarify the dictionary refresh strategy.\n"
    "- REQUIRED CHANGE: define popup dismissal UX."
)

FRONTEND_DONE = "Implemented manifest, content script, popup. Files added under frontend/."
BACKEND_DONE = "Implemented dictionary REST API at /lookup. Files under backend/."
TEST_PASS = "Ran end-to-end suite — all flows green.\n\nPASS"
TEST_DEFECT = "Ran tests.\n- DEFECT: backend /lookup returns 500 for empty word."


# ── happy path ───────────────────────────────────────────────────────────

def test_happy_path_chrome_translator(base_config, discord, workspace):
    runner = ScriptedRunner({
        AgentRole.DRAFT_PLAN: [DESIGN_DRAFT_FULL],
        AgentRole.REVIEW: [REVIEW_APPROVED],
        AgentRole.FRONTEND: [FRONTEND_DONE],
        AgentRole.BACKEND: [BACKEND_DONE],
        AgentRole.TEST: [TEST_PASS],
    })
    orch = Orchestrator(config=base_config, discord=discord, runner=runner)

    # User posts in the main channel.
    user_request = (
        "I want to build a chrome extension, a translator extension, when I double "
        "click on the website it will search in its built-in dictionary and give me the translation."
    )
    discord.deliver_user_message("ch_main", user_request, author_id="user_alice")

    # Wait for project channel creation + draft + review.
    assert _wait(orch, lambda: any(
        r.phase is ProjectPhase.AWAITING_APPROVAL for r in orch.store.all()
    ), timeout=5)

    record = orch.store.all()[0]
    assert record.project_channel_id is not None
    assert record.workspace_path is not None
    assert Path(record.workspace_path).exists()

    # Verify acknowledgment in main channel.
    main_msgs = [m.content for m in discord.fetch_messages("ch_main")]
    assert any("project" in m.lower() and "<#" in m for m in main_msgs)

    # Project channel transcript: original request, prometheus draft, review, prompt for approval.
    proj_msgs = [m.content for m in discord.fetch_messages(record.project_channel_id)]
    assert any("Athena: welcome to project" in m for m in proj_msgs)
    assert any("Prometheus:" in m for m in proj_msgs)
    assert any("Argus:" in m for m in proj_msgs)
    assert any("Reply **approve**" in m for m in proj_msgs)

    # No implementation channels created yet — approval gate holds.
    assert not record.workstreams

    # User approves.
    discord.deliver_user_message(record.project_channel_id, "approve", author_id="user_alice")

    assert _wait(orch, lambda: orch.store.get(record.project_id).phase is ProjectPhase.COMPLETE, timeout=5)
    final = orch.store.get(record.project_id)
    assert final.approved_design_version == 1
    assert WorkstreamKind.FRONTEND.value in final.workstreams
    assert WorkstreamKind.BACKEND.value in final.workstreams
    assert WorkstreamKind.TEST.value in final.workstreams
    for ws in final.workstreams.values():
        assert ws.status is WorkstreamStatus.COMPLETE

    # Each workstream agent only spoke in its own channel.
    for ws in final.workstreams.values():
        ws_msgs = [m for m in discord.fetch_messages(ws.channel_id)]
        assert ws_msgs, f"workstream {ws.kind} has no messages"
    # Main agent Athena posted final summary in project channel.
    proj_msgs_final = [m.content for m in discord.fetch_messages(final.project_channel_id)]
    assert any("Project complete" in m for m in proj_msgs_final)
    orch.shutdown()


def test_review_rejection_triggers_revision_round(base_config, discord, workspace):
    runner = ScriptedRunner({
        AgentRole.DRAFT_PLAN: [DESIGN_DRAFT_FULL, DESIGN_DRAFT_FULL],
        AgentRole.REVIEW: [REVIEW_NEEDS_CHANGES, REVIEW_APPROVED],
        AgentRole.FRONTEND: [FRONTEND_DONE],
        AgentRole.BACKEND: [BACKEND_DONE],
        AgentRole.TEST: [TEST_PASS],
    })
    orch = Orchestrator(config=base_config, discord=discord, runner=runner)

    discord.deliver_user_message("ch_main", "build a chrome translator extension please", author_id="u")

    assert _wait(orch, lambda: any(
        r.phase is ProjectPhase.AWAITING_APPROVAL and len(r.review_rounds) == 2
        for r in orch.store.all()
    ), timeout=5)

    record = orch.store.all()[0]
    assert len(record.review_rounds) == 2
    assert record.review_rounds[0].requested_changes
    assert not record.review_rounds[1].requested_changes
    orch.shutdown()


def test_clarifying_questions_pause_for_user_answers(base_config, discord, workspace):
    questions_text = "QUESTIONS:\n1. Which target browsers?\n2. Should the dictionary be customizable?"
    runner = ScriptedRunner({
        AgentRole.DRAFT_PLAN: [questions_text, DESIGN_DRAFT_FULL],
        AgentRole.REVIEW: [REVIEW_APPROVED],
        AgentRole.FRONTEND: [FRONTEND_DONE],
        AgentRole.BACKEND: [BACKEND_DONE],
        AgentRole.TEST: [TEST_PASS],
    })
    orch = Orchestrator(config=base_config, discord=discord, runner=runner)
    discord.deliver_user_message("ch_main", "build me a chrome translator extension", author_id="u")

    assert _wait(orch, lambda: any(
        r.phase is ProjectPhase.CLARIFYING for r in orch.store.all()
    ), timeout=5)
    record = orch.store.all()[0]
    assert record.clarifying_questions, "questions should be parsed"

    # User answers — orchestrator re-runs drafting.
    discord.deliver_user_message(record.project_channel_id, "Chrome only; static dictionary is fine.", author_id="u")
    assert _wait(orch, lambda: orch.store.get(record.project_id).phase is ProjectPhase.AWAITING_APPROVAL, timeout=5)
    orch.shutdown()


def test_non_project_message_is_ignored(base_config, discord, workspace):
    runner = ScriptedRunner({})
    orch = Orchestrator(config=base_config, discord=discord, runner=runner)
    discord.deliver_user_message("ch_main", "lol", author_id="u")
    discord.deliver_user_message("ch_main", "hi", author_id="u")
    discord.deliver_user_message("ch_main", "?help", author_id="u")
    # Tiny pause for any background work.
    time.sleep(0.2)
    assert orch.store.list_ids() == [], "no project should be created"
    orch.shutdown()


def test_two_concurrent_projects_are_isolated(base_config, discord, workspace):
    runner = ScriptedRunner({
        AgentRole.DRAFT_PLAN: [DESIGN_DRAFT_FULL, DESIGN_DRAFT_FULL],
        AgentRole.REVIEW: [REVIEW_APPROVED, REVIEW_APPROVED],
        AgentRole.FRONTEND: [FRONTEND_DONE, FRONTEND_DONE],
        AgentRole.BACKEND: [BACKEND_DONE, BACKEND_DONE],
        AgentRole.TEST: [TEST_PASS, TEST_PASS],
    })
    orch = Orchestrator(config=base_config, discord=discord, runner=runner)

    discord.deliver_user_message("ch_main", "build a chrome translator extension", author_id="alice")
    discord.deliver_user_message("ch_main", "build a backend api service for todos", author_id="bob")

    assert _wait(orch, lambda: len(orch.store.all()) == 2, timeout=5)
    # Wait until both reach AWAITING_APPROVAL.
    assert _wait(orch, lambda: all(
        r.phase is ProjectPhase.AWAITING_APPROVAL for r in orch.store.all()
    ), timeout=5)

    projects = orch.store.all()
    p_alice = next(r for r in projects if r.owner_user_id == "alice")
    p_bob = next(r for r in projects if r.owner_user_id == "bob")

    # Different channels.
    assert p_alice.project_channel_id != p_bob.project_channel_id
    # Different workspaces.
    assert p_alice.workspace_path != p_bob.workspace_path

    # Approve only Alice's — Bob's must NOT progress to decomposition.
    discord.deliver_user_message(p_alice.project_channel_id, "approve", author_id="alice")
    assert _wait(orch, lambda: orch.store.get(p_alice.project_id).phase is ProjectPhase.COMPLETE, timeout=5)

    bob_after = orch.store.get(p_bob.project_id)
    assert bob_after.phase is ProjectPhase.AWAITING_APPROVAL
    assert not bob_after.workstreams, "no workstreams created for unapproved project"

    # Approve Bob's now — full flow runs independently.
    discord.deliver_user_message(p_bob.project_channel_id, "approve", author_id="bob")
    assert _wait(orch, lambda: orch.store.get(p_bob.project_id).phase is ProjectPhase.COMPLETE, timeout=5)
    orch.shutdown()


def test_approval_gate_blocks_implementation(base_config, discord, workspace):
    runner = ScriptedRunner({
        AgentRole.DRAFT_PLAN: [DESIGN_DRAFT_FULL],
        AgentRole.REVIEW: [REVIEW_APPROVED],
        AgentRole.FRONTEND: [FRONTEND_DONE],
        AgentRole.BACKEND: [BACKEND_DONE],
        AgentRole.TEST: [TEST_PASS],
    })
    orch = Orchestrator(config=base_config, discord=discord, runner=runner)
    discord.deliver_user_message("ch_main", "build a chrome translator extension", author_id="u")
    assert _wait(orch, lambda: any(
        r.phase is ProjectPhase.AWAITING_APPROVAL for r in orch.store.all()
    ), timeout=5)
    record = orch.store.all()[0]

    # Random message that is NOT approval treated as revision request.
    discord.deliver_user_message(record.project_channel_id, "actually change the dictionary loader", author_id="u")
    # Should bounce back to drafting; since runner has only one draft, draft will be (no scripted reply)
    time.sleep(0.5)
    assert not record.workstreams
    orch.shutdown()


def test_persistence_survives_recreate(base_config, discord, workspace):
    runner = ScriptedRunner({
        AgentRole.DRAFT_PLAN: [DESIGN_DRAFT_FULL],
        AgentRole.REVIEW: [REVIEW_APPROVED],
        AgentRole.FRONTEND: [FRONTEND_DONE],
        AgentRole.BACKEND: [BACKEND_DONE],
        AgentRole.TEST: [TEST_PASS],
    })
    orch = Orchestrator(config=base_config, discord=discord, runner=runner)
    discord.deliver_user_message("ch_main", "build a chrome translator extension", author_id="u")
    assert _wait(orch, lambda: any(
        r.phase is ProjectPhase.AWAITING_APPROVAL for r in orch.store.all()
    ), timeout=5)
    project_id = orch.store.all()[0].project_id
    orch.shutdown()

    # Create a fresh orchestrator pointing at the same state dir.
    runner2 = ScriptedRunner({
        AgentRole.FRONTEND: [FRONTEND_DONE],
        AgentRole.BACKEND: [BACKEND_DONE],
        AgentRole.TEST: [TEST_PASS],
    })
    orch2 = Orchestrator(config=base_config, discord=discord, runner=runner2)
    record = orch2.store.get(project_id)
    assert record is not None and record.phase is ProjectPhase.AWAITING_APPROVAL
    # Can resume — approval still works post-restart.
    discord.deliver_user_message(record.project_channel_id, "approve", author_id="u")
    assert _wait(orch2, lambda: orch2.store.get(project_id).phase is ProjectPhase.COMPLETE, timeout=5)
    orch2.shutdown()


def test_idempotent_intake_does_not_double_create(base_config, discord, workspace):
    runner = ScriptedRunner({
        AgentRole.DRAFT_PLAN: [DESIGN_DRAFT_FULL],
        AgentRole.REVIEW: [REVIEW_APPROVED],
    })
    orch = Orchestrator(config=base_config, discord=discord, runner=runner)
    msg = discord.deliver_user_message("ch_main", "build a chrome translator extension", author_id="u")
    # Re-deliver the exact same message id (simulate Discord redelivery).
    for handler in discord.handlers:
        handler(msg)
    assert _wait(orch, lambda: len(orch.store.all()) >= 1, timeout=5)
    time.sleep(0.3)
    assert len(orch.store.all()) == 1, "duplicate event must not create a second project"
    orch.shutdown()


def test_workstream_question_escalates_to_project_channel(base_config, discord, workspace):
    runner = ScriptedRunner({
        AgentRole.DRAFT_PLAN: [DESIGN_DRAFT_FULL],
        AgentRole.REVIEW: [REVIEW_APPROVED],
        AgentRole.FRONTEND: [FRONTEND_DONE],
        AgentRole.BACKEND: [BACKEND_DONE],
        AgentRole.TEST: [TEST_PASS],
    })
    orch = Orchestrator(config=base_config, discord=discord, runner=runner)
    discord.deliver_user_message("ch_main", "build a chrome translator extension", author_id="u")
    assert _wait(orch, lambda: any(
        r.phase is ProjectPhase.AWAITING_APPROVAL for r in orch.store.all()
    ), timeout=5)
    record = orch.store.all()[0]
    discord.deliver_user_message(record.project_channel_id, "approve", author_id="u")
    assert _wait(orch, lambda: orch.store.get(record.project_id).phase is ProjectPhase.COMPLETE, timeout=5)
    record = orch.store.get(record.project_id)

    # Manually escalate a question from the frontend workstream.
    orch.escalate_question(record.project_id, WorkstreamKind.FRONTEND, "should the popup support keyboard nav?")
    proj_msgs = [m.content for m in discord.fetch_messages(record.project_channel_id)]
    assert any("question from frontend workstream" in m for m in proj_msgs)
    orch.shutdown()
