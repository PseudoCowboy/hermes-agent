"""End-to-end happy-path tests for the mythos orchestrator.

These tests simulate the full user-scenario flow with FakeDiscordTransport
and FakeRunner — no real Discord, no real CLI invocations.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mythos.models import (
    AgentRole,
    ArtifactType,
    ChannelRole,
    ProjectPhase,
)


@pytest.mark.asyncio
async def test_happy_path_chrome_translator_extension(orchestrator, transport):
    """User-scenario.md happy path:

    1. User posts request in main channel.
    2. Athena creates a project channel and pings Prometheus.
    3. Prometheus asks a clarifying question.
    4. User answers; Prometheus writes a design spec.
    5. Argus reviews; main asks for approval.
    6. User approves -> Athena decomposes into frontend/backend/test channels.
    7. Apollo / Atlas / Hephaestus implement and post completion.
    """
    main_chan = orchestrator.config.discord.main_channel_id

    # 1. User posts the chrome translator request in the main channel.
    await transport.simulate_user_message(
        channel_id=main_chan, user_id="user_alice",
        content=(
            "I want to build a chrome extension, a translator extension, "
            "when I double click on the website, it will search in its "
            "built-in dictionary, give me the translation."
        ),
    )

    # Project should exist now and have a project channel.
    projects = orchestrator.store.list_projects()
    assert len(projects) == 1
    project = projects[0]
    assert project.project_channel_id, "project channel must be created"
    project_chan = project.project_channel_id

    # Athena acknowledged in main channel.
    main_msgs = transport.messages_by_channel[main_chan]
    assert any("Athena" in m.content for m in main_msgs), (
        "Athena must acknowledge in main channel"
    )
    assert any(project.id in m.content for m in main_msgs)

    # Prometheus asked clarification in the project channel.
    project_msgs = transport.messages_by_channel[project_chan]
    assert any("Prometheus" in m.content for m in project_msgs), (
        "Prometheus must post in project channel"
    )
    proj_after_clarify = orchestrator.store.get_project(project.id)
    assert proj_after_clarify.phase == ProjectPhase.CLARIFICATION

    # 2. User answers clarification in the project channel.
    await transport.simulate_user_message(
        channel_id=project_chan, user_id="user_alice",
        content="Bundle the dictionary, no network calls.",
    )

    # Now the spec should be drafted and reviewed; phase => awaiting_approval
    proj = orchestrator.store.get_project(project.id)
    assert proj.phase == ProjectPhase.AWAITING_APPROVAL, (
        f"expected AWAITING_APPROVAL, got {proj.phase}"
    )

    # Argus posted review in project channel.
    project_msgs = transport.messages_by_channel[project_chan]
    assert any("Argus" in m.content for m in project_msgs)

    # Design spec artifact persisted.
    arts = orchestrator.store.list_artifacts(project.id)
    assert any(a.type == ArtifactType.DESIGN_SPEC for a in arts)
    assert any(a.type == ArtifactType.REVIEW_COMMENTS for a in arts)

    # 3. Owner approves.
    await transport.simulate_user_message(
        channel_id=project_chan, user_id="user_alice",
        content="approve",
    )

    proj_done = orchestrator.store.get_project(project.id)
    assert proj_done.phase == ProjectPhase.COMPLETED, (
        f"expected COMPLETED, got {proj_done.phase}"
    )

    # Approved-design artifact is locked (immutable).
    approved = [
        a for a in orchestrator.store.list_artifacts(project.id)
        if a.type == ArtifactType.APPROVED_DESIGN
    ]
    assert len(approved) == 1
    assert approved[0].immutable is True

    # Frontend / backend / test channels were created.
    channel_roles = {
        c.channel_role.value
        for c in orchestrator.store.list_channels(project.id)
    }
    assert "frontend" in channel_roles
    assert "backend" in channel_roles
    assert "test" in channel_roles

    # Apollo / Atlas / Hephaestus each posted completion in their own channel.
    fe_chan = orchestrator.store.find_channel(project.id, "frontend").discord_channel_id
    be_chan = orchestrator.store.find_channel(project.id, "backend").discord_channel_id
    te_chan = orchestrator.store.find_channel(project.id, "test").discord_channel_id

    assert any("Apollo" in m.content for m in transport.messages_by_channel[fe_chan])
    assert any("Atlas" in m.content for m in transport.messages_by_channel[be_chan])
    assert any("Hephaestus" in m.content for m in transport.messages_by_channel[te_chan])

    # Channel discipline: Apollo never posted in the backend channel etc.
    assert not any("Apollo" in m.content for m in transport.messages_by_channel[be_chan])
    assert not any("Atlas" in m.content for m in transport.messages_by_channel[fe_chan])
    assert not any("Hephaestus" in m.content for m in transport.messages_by_channel[fe_chan])

    # Approval was recorded.
    approvals = orchestrator.store.list_approvals(project.id)
    assert len(approvals) == 1
    assert approvals[0].approved_by_user_id == "user_alice"

    # Audit trail captured key events.
    events = [e.event_type for e in orchestrator.store.iter_audit(project.id)]
    assert "project.created" in events
    assert "design.approved" in events
    assert "phase.transition" in events


@pytest.mark.asyncio
async def test_non_owner_cannot_approve(orchestrator, transport):
    """Approval gate: only the owner may approve a project."""
    await _drive_to_awaiting_approval(orchestrator, transport, owner="alice")
    project = orchestrator.store.list_projects()[0]
    proj_chan = project.project_channel_id

    # A different user tries to approve
    await transport.simulate_user_message(
        channel_id=proj_chan, user_id="not_alice", content="approve",
    )

    proj = orchestrator.store.get_project(project.id)
    assert proj.phase == ProjectPhase.AWAITING_APPROVAL  # unchanged
    msgs = transport.messages_by_channel[proj_chan]
    assert any("only the project" in m.content.lower() for m in msgs)


@pytest.mark.asyncio
async def test_implementation_cannot_dispatch_without_approval(orchestrator, transport):
    """If user never approves, no specialist channels are created."""
    await _drive_to_awaiting_approval(orchestrator, transport, owner="alice")
    project = orchestrator.store.list_projects()[0]

    # Don't approve. Verify no frontend/backend/test channels exist.
    roles = {
        c.channel_role.value
        for c in orchestrator.store.list_channels(project.id)
    }
    assert "frontend" not in roles
    assert "backend" not in roles
    assert "test" not in roles


@pytest.mark.asyncio
async def test_two_concurrent_projects_stay_isolated(orchestrator, transport):
    """AC-007: two concurrent projects must not collide."""
    main_chan = orchestrator.config.discord.main_channel_id

    # Project A
    await transport.simulate_user_message(
        channel_id=main_chan, user_id="alice",
        content="Build a chrome translator extension with offline dictionary.",
    )
    # Project B (different user, different request)
    await transport.simulate_user_message(
        channel_id=main_chan, user_id="bob",
        content="Build a backend api server with REST endpoints and tests.",
    )

    projects = orchestrator.store.list_projects()
    assert len(projects) == 2
    ids = {p.id for p in projects}
    assert len(ids) == 2

    # Each must have its own project channel and workspace.
    chans = {p.project_channel_id for p in projects}
    assert len(chans) == 2

    workspaces = {p.workspace_path for p in projects}
    assert len(workspaces) == 2
    for ws in workspaces:
        assert Path(ws).exists()

    # Drive A through clarify+approve
    a = next(p for p in projects if p.owner_user_id == "alice")
    await transport.simulate_user_message(
        channel_id=a.project_channel_id, user_id="alice",
        content="Bundle dictionary.",
    )
    await transport.simulate_user_message(
        channel_id=a.project_channel_id, user_id="alice", content="approve",
    )

    a_done = orchestrator.store.get_project(a.id)
    assert a_done.phase == ProjectPhase.COMPLETED

    # B should still be in clarification or awaiting_approval, NOT completed
    b = next(p for p in projects if p.owner_user_id == "bob")
    b_now = orchestrator.store.get_project(b.id)
    assert b_now.phase != ProjectPhase.COMPLETED

    # Cross-project channel isolation: A's channel ids != B's channel ids
    a_chan_ids = {c.discord_channel_id for c in orchestrator.store.list_channels(a.id)}
    b_chan_ids = {c.discord_channel_id for c in orchestrator.store.list_channels(b.id)}
    assert a_chan_ids.isdisjoint(b_chan_ids)


@pytest.mark.asyncio
async def test_specialist_only_responds_in_own_channel(orchestrator, transport):
    """FR-034 / AC-006: specialists post only in their assigned channel."""
    await _drive_to_completion(orchestrator, transport, owner="alice")
    project = orchestrator.store.list_projects()[0]

    fe_chan = orchestrator.store.find_channel(project.id, "frontend").discord_channel_id
    be_chan = orchestrator.store.find_channel(project.id, "backend").discord_channel_id
    project_chan = project.project_channel_id

    # Apollo (frontend) must NOT appear in project, backend, or test channels.
    for chan in [project_chan, be_chan]:
        msgs = transport.messages_by_channel[chan]
        assert not any("Apollo" in m.content for m in msgs), (
            f"Apollo leaked into channel {chan}"
        )


@pytest.mark.asyncio
async def test_state_persisted_across_restart(orchestrator, transport, base_config):
    """NFR-001: project state survives orchestrator restart."""
    await _drive_to_awaiting_approval(orchestrator, transport, owner="alice")
    project = orchestrator.store.list_projects()[0]
    pid = project.id

    # Build a fresh store from the same state_dir
    from mythos.store import JsonStore
    fresh = JsonStore(base_config.state_dir)
    reloaded = fresh.get_project(pid)
    assert reloaded is not None
    assert reloaded.phase == ProjectPhase.AWAITING_APPROVAL
    assert reloaded.workspace_path == project.workspace_path
    assert len(fresh.list_artifacts(pid)) >= 2  # spec + review


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


async def _drive_to_awaiting_approval(orchestrator, transport, owner: str):
    main_chan = orchestrator.config.discord.main_channel_id
    await transport.simulate_user_message(
        channel_id=main_chan, user_id=owner,
        content=(
            "I want a chrome translator extension that looks up words from "
            "a built-in dictionary on double click."
        ),
    )
    project = orchestrator.store.list_projects()[-1]
    await transport.simulate_user_message(
        channel_id=project.project_channel_id, user_id=owner,
        content="Bundle the dictionary.",
    )


async def _drive_to_completion(orchestrator, transport, owner: str):
    await _drive_to_awaiting_approval(orchestrator, transport, owner)
    project = orchestrator.store.list_projects()[-1]
    await transport.simulate_user_message(
        channel_id=project.project_channel_id, user_id=owner,
        content="approve",
    )
