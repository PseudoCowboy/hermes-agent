"""End-to-end happy-path test that exercises every Mythos component."""

from __future__ import annotations

import pytest

from mythos.discord_io import InMemoryDiscordIO
from mythos.orchestrator import MythosOrchestrator
from mythos.project_manager import (
    BACKEND_CHANNEL,
    FRONTEND_CHANNEL,
    GENERAL_CHANNEL,
    TEST_CHANNEL,
)
from mythos.roles import Role
from mythos.state import ProjectPhase


@pytest.mark.asyncio
async def test_happy_path_kickoff_to_completion(
    cfg, discord: InMemoryDiscordIO, orchestrator: MythosOrchestrator
):
    # 1. User drops an idea in #main.
    await discord.inject_user_message(
        cfg.main_channel_id,
        "I want to build a Chrome extension, a translator, "
        "double-click on a word, look it up in a built-in dictionary, "
        "show the translation.",
    )
    # The kickoff handler returns immediately and the draft phase runs
    # in the background. Wait for it.
    await orchestrator.await_pending()

    # 2. A project should exist.
    projects = orchestrator.pm.list_projects()
    assert len(projects) == 1
    rec = projects[0]
    slug = rec.state.slug

    # 3. Hermes posted an ack in #main.
    main_msgs = discord.channel_messages(cfg.main_channel_id)
    assert any("Hermes" in m and slug in m for m in main_msgs), main_msgs

    # 4. A project category + general channel were created.
    assert rec.state.category_id is not None
    assert discord.category_name(rec.state.category_id).startswith("mythos-")
    general_id = rec.state.channels[GENERAL_CHANNEL]
    general_name = discord.channel_name(general_id)
    assert general_name and general_name.endswith("-general")

    # 5. Prometheus drafted, Argus reviewed, Hermes asked for approval.
    general_msgs = discord.channel_messages(general_id)
    joined = "\n---\n".join(general_msgs)
    assert "Prometheus" in joined
    assert "Translator Extension Spec" in joined
    assert "Argus" in joined
    assert "APPROVED" in joined  # The review verdict
    assert rec.state.phase == ProjectPhase.AWAITING_USER

    # 6. Spec was persisted to disk.
    spec_text = rec.workspace.read_latest_spec()
    assert spec_text is not None
    assert "Translator Extension Spec" in spec_text
    assert (rec.workspace.spec_dir / "spec-v1.md").exists()

    # 7. User approves.
    await discord.inject_user_message(general_id, "approve")
    await orchestrator.await_pending()

    # 8. Decomposition + specialist channels.
    assert FRONTEND_CHANNEL in rec.state.channels
    assert BACKEND_CHANNEL in rec.state.channels
    assert TEST_CHANNEL in rec.state.channels

    fe_id = rec.state.channels[FRONTEND_CHANNEL]
    be_id = rec.state.channels[BACKEND_CHANNEL]
    te_id = rec.state.channels[TEST_CHANNEL]

    fe = "\n".join(discord.channel_messages(fe_id))
    be = "\n".join(discord.channel_messages(be_id))
    te = "\n".join(discord.channel_messages(te_id))

    # 9. Each specialist posted in its own channel only.
    assert "Apollo" in fe
    assert "Atlas" in be
    assert "Hephaestus" in te

    # 10. Cross-channel discipline: Apollo's name does not appear in
    # backend or test channels (and vice versa).
    assert "Apollo" not in be and "Apollo" not in te
    assert "Atlas" not in fe and "Atlas" not in te
    assert "Hephaestus" not in fe and "Hephaestus" not in be

    # 11. Hermes announced specialist channel opening + completion in #general.
    general_msgs = "\n".join(discord.channel_messages(general_id))
    assert "Specialist channels opened" in general_msgs
    assert "specialists report complete" in general_msgs

    # 12. Phase progressed all the way.
    assert rec.state.phase == ProjectPhase.COMPLETE
