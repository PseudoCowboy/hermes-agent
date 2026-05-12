"""Integration tests for mythos.

Drive the orchestrator through the happy path with a fake Discord and a
scripted CLI runner. No real Discord, no real LLM calls.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mythos.config import MythosConfig
from mythos.discord_adapter import FakeDiscordAdapter
from mythos.models import Discipline, ProjectState
from mythos.orchestrator import MythosOrchestrator
from mythos.store import Store

from tests.mythos.scripted_runner import ScriptedRunner


def _build(tmp_state_dir, runner=None, role_tokens=None):
    cfg = MythosConfig.from_env_and_yaml(None)
    cfg.state_dir = Path(tmp_state_dir)
    cfg.main_channel_id = 5_000  # arbitrary
    cfg.discord_guild_id = 1
    cfg.discord_bot_token = "x"
    cfg.discord_role_bot_tokens = dict(role_tokens or {})
    store = Store(cfg.state_dir / "mythos.sqlite")
    discord = FakeDiscordAdapter(bot_user_id=99)
    runner = runner or ScriptedRunner()
    orch = MythosOrchestrator(cfg, store, discord, runner=runner)
    return cfg, store, discord, orch, runner


def _project_completion_messages(discord, channel_id):
    return [
        m for m in discord.messages_by_channel.get(channel_id, [])
        if "complete. All specialist workstreams finished" in m["content"]
    ]


@pytest.mark.asyncio
async def test_main_channel_post_creates_isolated_project_channel(tmp_state_dir):
    cfg, store, discord, orch, runner = _build(tmp_state_dir)
    await orch.start()

    # User posts in main.
    await discord.simulate_user_message(
        channel_id=cfg.main_channel_id, user_id=42,
        content="I want to build a Chrome translator extension.",
    )
    # Let the asyncio.create_task drain.
    for _ in range(20):
        await asyncio.sleep(0)

    # An ack should be in main.
    main_msgs = discord.channel_transcript(cfg.main_channel_id)
    assert any("Athena here" in m for m in main_msgs), main_msgs

    # A new channel should exist with seed message.
    project_channels = [cid for cid, info in discord.channels.items()
                        if info["name"].startswith("proj-")]
    assert len(project_channels) == 1
    pcid = project_channels[0]
    pmsgs = discord.channel_transcript(pcid)
    assert any("Seed request" in m for m in pmsgs)
    assert any("@Prometheus" in m for m in pmsgs)

    # Project is recorded in the store.
    projects = store.list_projects()
    assert len(projects) == 1
    assert projects[0].owner_user_id == 42
    assert projects[0].project_channel_id == pcid

    await orch.stop()


@pytest.mark.asyncio
async def test_full_happy_path_drafts_reviews_approves_decomposes(tmp_state_dir):
    cfg, store, discord, orch, runner = _build(tmp_state_dir)
    await orch.start()

    # 1. User posts
    await discord.simulate_user_message(
        channel_id=cfg.main_channel_id, user_id=42,
        content="Build a chrome translator extension that looks up double-clicked words in a built-in dictionary.",
    )
    # Drain drafting + review (multiple awaits).
    for _ in range(50):
        await asyncio.sleep(0)

    project = store.list_projects()[0]
    pcid = project.project_channel_id
    pmsgs = discord.channel_transcript(pcid)

    # Draft spec posted by Prometheus.
    assert any("draft spec v1" in m for m in pmsgs), pmsgs[-3:]
    # Review posted by Argus.
    assert any("Argus" in m and "review" in m.lower() for m in pmsgs), pmsgs[-3:]
    # Athena requests approval.
    assert any("approve" in m.lower() for m in pmsgs), pmsgs[-3:]

    # 2. Owner approves.
    await discord.simulate_user_message(
        channel_id=pcid, user_id=42, content="approve",
    )
    for _ in range(80):
        await asyncio.sleep(0)

    project = store.get_project(project.project_id)
    assert project.state == ProjectState.COMPLETE, project.state
    assert project.completed_disciplines == ["frontend", "backend", "test"]

    # 3. Discipline channels created.
    assert "frontend" in project.discipline_channels
    assert "backend" in project.discipline_channels
    assert "test" in project.discipline_channels

    # 4. Each specialist posted a "WORK COMPLETE" line in its own channel.
    fe = discord.channel_transcript(project.discipline_channels["frontend"])
    be = discord.channel_transcript(project.discipline_channels["backend"])
    te = discord.channel_transcript(project.discipline_channels["test"])
    assert any("FRONTEND WORK COMPLETE" in m for m in fe), fe
    assert any("BACKEND WORK COMPLETE" in m for m in be), be
    assert any("TEST WORK COMPLETE" in m for m in te), te

    # 5. Per-channel confinement: frontend completion did NOT leak to backend.
    assert not any("FRONTEND WORK COMPLETE" in m for m in be)
    assert not any("BACKEND WORK COMPLETE" in m for m in fe)

    # 6. Athena announced final project completion exactly once.
    completion_msgs = _project_completion_messages(discord, project.project_channel_id)
    assert len(completion_msgs) == 1

    # 7. Approval is recorded.
    approval = store.get_approval(project.project_id)
    assert approval is not None
    assert approval.user_id == 42

    await orch.stop()


@pytest.mark.asyncio
async def test_two_concurrent_projects_stay_isolated(tmp_state_dir):
    cfg, store, discord, orch, runner = _build(tmp_state_dir)
    await orch.start()

    # User A posts
    await discord.simulate_user_message(
        channel_id=cfg.main_channel_id, user_id=10,
        content="Build a chrome translator extension.", author_name="alice",
    )
    # User B posts a different project at the same time.
    await discord.simulate_user_message(
        channel_id=cfg.main_channel_id, user_id=20,
        content="Build a chrome translator extension for Spanish.", author_name="bob",
    )
    for _ in range(80):
        await asyncio.sleep(0)

    projects = store.list_projects()
    assert len(projects) == 2
    p1, p2 = projects
    assert p1.project_id != p2.project_id
    assert p1.project_channel_id != p2.project_channel_id

    # Approve only project 1.
    await discord.simulate_user_message(
        channel_id=p1.project_channel_id, user_id=p1.owner_user_id, content="approve",
    )
    for _ in range(80):
        await asyncio.sleep(0)

    p1 = store.get_project(p1.project_id)
    p2 = store.get_project(p2.project_id)

    assert p1.state == ProjectState.COMPLETE
    assert p1.completed_disciplines == ["frontend", "backend", "test"]
    # Project 2 was NOT touched by the approval.
    assert p2.state == ProjectState.AWAITING_APPROVAL
    assert p2.discipline_channels == {}

    # Project 1's discipline channels do not appear in project 2.
    p1_disc_ids = set(p1.discipline_channels.values())
    p2_msgs_anywhere = []
    for cid in [p2.project_channel_id]:
        p2_msgs_anywhere.extend(discord.channel_transcript(cid))
    for m in p2_msgs_anywhere:
        for cid in p1_disc_ids:
            assert str(cid) not in m  # discipline channel ids not echoed

    await orch.stop()


@pytest.mark.asyncio
async def test_role_bot_tokens_author_agent_messages(tmp_state_dir):
    role_tokens = {
        "prometheus": "prom-token",
        "argus": "argus-token",
        "apollo": "apollo-token",
        "atlas": "atlas-token",
        "hephaestus": "hephaestus-token",
    }
    cfg, store, discord, orch, runner = _build(tmp_state_dir, role_tokens=role_tokens)
    await orch.start()

    assert set(discord.role_bot_user_ids) == set(role_tokens)

    await discord.simulate_user_message(
        channel_id=cfg.main_channel_id,
        user_id=42,
        content="Build a chrome translator extension that uses a bundled dictionary.",
    )
    for _ in range(50):
        await asyncio.sleep(0)

    project = store.list_projects()[0]
    await discord.simulate_user_message(
        channel_id=project.project_channel_id,
        user_id=42,
        content="approve",
    )
    for _ in range(80):
        await asyncio.sleep(0)

    project = store.get_project(project.project_id)
    project_msgs = discord.messages_by_channel[project.project_channel_id]
    assert any(
        m.get("author_role") == "prometheus" and "draft spec v1" in m["content"]
        for m in project_msgs
    )
    assert any(
        m.get("author_role") == "argus" and "review of spec" in m["content"]
        for m in project_msgs
    )
    assert any(
        m.get("author_role") is None and "Athena here" in m["content"]
        for m in discord.messages_by_channel[cfg.main_channel_id]
    )

    expected_specialist_roles = {
        "frontend": "apollo",
        "backend": "atlas",
        "test": "hephaestus",
    }
    for discipline, role in expected_specialist_roles.items():
        channel_id = project.discipline_channels[discipline]
        assert any(
            m.get("author_role") == role and "WORK COMPLETE" in m["content"]
            for m in discord.messages_by_channel[channel_id]
        )

    await orch.stop()


@pytest.mark.asyncio
async def test_duplicate_completion_markers_are_idempotent(tmp_state_dir):
    cfg, store, discord, orch, runner = _build(tmp_state_dir)
    await orch.start()

    await discord.simulate_user_message(cfg.main_channel_id, user_id=42, content="translator")
    for _ in range(50):
        await asyncio.sleep(0)
    project = store.list_projects()[0]
    await discord.simulate_user_message(project.project_channel_id, user_id=42, content="approve")
    for _ in range(80):
        await asyncio.sleep(0)

    project = store.get_project(project.project_id)
    before = list(_project_completion_messages(discord, project.project_channel_id))
    assert len(before) == 1

    await orch._mark_specialist_complete(
        project.project_id,
        Discipline.FRONTEND,
        "Frontend cleanup done.\nFRONTEND WORK COMPLETE",
    )

    after = _project_completion_messages(discord, project.project_channel_id)
    assert after == before

    await orch.stop()


@pytest.mark.asyncio
async def test_revise_loop_is_bounded(tmp_state_dir):
    cfg, store, discord, orch, runner = _build(tmp_state_dir)
    cfg.max_review_iterations = 2
    await orch.start()

    await discord.simulate_user_message(
        channel_id=cfg.main_channel_id, user_id=42, content="translator extension",
    )
    for _ in range(50):
        await asyncio.sleep(0)
    project = store.list_projects()[0]
    pcid = project.project_channel_id

    # Owner asks to revise twice; the third should be told the cap was hit.
    await discord.simulate_user_message(channel_id=pcid, user_id=42, content="revise add error handling")
    for _ in range(60):
        await asyncio.sleep(0)
    await discord.simulate_user_message(channel_id=pcid, user_id=42, content="revise tighten scope")
    for _ in range(60):
        await asyncio.sleep(0)

    project = store.get_project(project.project_id)
    msgs = discord.channel_transcript(pcid)
    # When iteration cap is reached, the next 'revise' should produce a max-rounds message.
    await discord.simulate_user_message(channel_id=pcid, user_id=42, content="revise more cleanup")
    for _ in range(20):
        await asyncio.sleep(0)
    msgs = discord.channel_transcript(pcid)
    assert any("max revision rounds" in m.lower() for m in msgs), msgs[-3:]

    await orch.stop()


@pytest.mark.asyncio
async def test_only_owner_can_approve(tmp_state_dir):
    cfg, store, discord, orch, runner = _build(tmp_state_dir)
    await orch.start()
    await discord.simulate_user_message(cfg.main_channel_id, user_id=42, content="translator extension idea")
    for _ in range(50):
        await asyncio.sleep(0)
    project = store.list_projects()[0]

    # Random user tries to approve.
    await discord.simulate_user_message(project.project_channel_id, user_id=999, content="approve")
    for _ in range(20):
        await asyncio.sleep(0)
    project = store.get_project(project.project_id)
    assert project.state == ProjectState.AWAITING_APPROVAL  # NOT moved
    msgs = discord.channel_transcript(project.project_channel_id)
    assert any("only the project owner" in m for m in msgs)

    await orch.stop()


@pytest.mark.asyncio
async def test_specialist_confinement_only_responds_in_own_channel(tmp_state_dir):
    cfg, store, discord, orch, runner = _build(tmp_state_dir)
    await orch.start()
    await discord.simulate_user_message(cfg.main_channel_id, user_id=42, content="translator")
    for _ in range(50):
        await asyncio.sleep(0)
    project = store.list_projects()[0]
    await discord.simulate_user_message(project.project_channel_id, user_id=42, content="approve")
    for _ in range(80):
        await asyncio.sleep(0)
    project = store.get_project(project.project_id)

    # Now post a message in the project parent channel that mentions "Apollo".
    pre_fe = list(discord.channel_transcript(project.discipline_channels["frontend"]))
    await discord.simulate_user_message(
        project.project_channel_id, user_id=42, content="@Apollo can you also do dark mode?",
    )
    for _ in range(20):
        await asyncio.sleep(0)
    post_fe = discord.channel_transcript(project.discipline_channels["frontend"])
    # Apollo did NOT post in the frontend channel because the trigger came from
    # the parent channel — orchestrator only routes to specialists when the
    # message lands in the discipline sub-channel itself.
    assert pre_fe == post_fe

    await orch.stop()
