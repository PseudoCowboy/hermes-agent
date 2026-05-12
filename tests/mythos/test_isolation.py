"""Project isolation guarantees: per-channel agent confinement, no cross-talk."""

from __future__ import annotations

import pytest

from mythos.discord_io import InMemoryDiscordIO
from mythos.orchestrator import MythosOrchestrator
from mythos.project_manager import GENERAL_CHANNEL


@pytest.mark.asyncio
async def test_two_concurrent_projects_are_isolated(
    cfg, discord: InMemoryDiscordIO, orchestrator: MythosOrchestrator
):
    """Run two projects through approval; verify channels and slugs differ."""
    await discord.inject_user_message(
        cfg.main_channel_id, "build a chrome translator extension"
    )
    await orchestrator.await_pending()
    await discord.inject_user_message(
        cfg.main_channel_id, "build a markdown notebook editor"
    )
    await orchestrator.await_pending()

    projects = orchestrator.pm.list_projects()
    assert len(projects) == 2
    a, b = projects[0], projects[1]
    assert a.state.slug != b.state.slug
    assert a.state.category_id != b.state.category_id
    # No channel id is reused across projects.
    a_chans = set(a.state.channels.values())
    b_chans = set(b.state.channels.values())
    assert a_chans.isdisjoint(b_chans)
    # Each workspace is on its own dir.
    assert a.workspace.root != b.workspace.root


@pytest.mark.asyncio
async def test_message_in_unrelated_channel_is_ignored(
    cfg, discord: InMemoryDiscordIO, orchestrator: MythosOrchestrator
):
    """A message in a channel Mythos does not own must not provoke a reply."""
    UNRELATED = 9999
    await discord.inject_user_message(UNRELATED, "hi mythos, are you there?")
    # Nothing should have been posted anywhere.
    assert not discord.channel_messages(UNRELATED)
    assert not discord.channel_messages(cfg.main_channel_id)


@pytest.mark.asyncio
async def test_user_reply_in_general_channel_during_drafting_is_treated_as_clarification(
    cfg, discord: InMemoryDiscordIO, orchestrator: MythosOrchestrator
):
    """During DRAFTING (rare race), a user follow-up should re-fire the draft."""
    await discord.inject_user_message(cfg.main_channel_id, "build a calorie tracker")
    await orchestrator.await_pending()
    rec = orchestrator.pm.list_projects()[0]
    general_id = rec.state.channels[GENERAL_CHANNEL]
    # Project should be AWAITING_USER now; reply with a change request.
    await discord.inject_user_message(
        general_id,
        "actually I want it to support multi-user accounts as well, "
        "please revise the spec to add that",
    )
    await orchestrator.await_pending()
    # Approval round must have advanced.
    assert rec.state.approval_round >= 1
