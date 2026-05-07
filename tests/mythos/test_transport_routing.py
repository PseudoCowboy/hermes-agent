"""Tests for the FakeDiscordTransport itself (so we trust the test harness)
and basic message routing rules in isolation."""

from __future__ import annotations

import asyncio

import pytest

from mythos.discord_bot import FakeDiscordTransport, IncomingMessage


@pytest.mark.asyncio
async def test_fake_transport_records_sends():
    t = FakeDiscordTransport(main_channel_id="main")
    await t.start()
    cid = await t.create_project_channel("p1", "prj-foo")
    mid = await t.send(cid, "hello")
    assert mid
    assert t.messages_by_channel[cid][0].content == "hello"


@pytest.mark.asyncio
async def test_fake_transport_invokes_handler():
    t = FakeDiscordTransport(main_channel_id="main")
    captured = []

    async def handler(ev: IncomingMessage):
        captured.append(ev)

    t.set_message_handler(handler)
    await t.simulate_user_message("main", "u1", "hi", "alice")
    assert len(captured) == 1
    assert captured[0].content == "hi"


@pytest.mark.asyncio
async def test_subchannels_grouped_per_project():
    t = FakeDiscordTransport(main_channel_id="main")
    proj_chan = await t.create_project_channel("p1", "prj-a")
    fe = await t.create_subchannel("p1", proj_chan, "prj-a-frontend")
    be = await t.create_subchannel("p1", proj_chan, "prj-a-backend")
    assert fe != be
    assert t.channels[fe]["parent"] == proj_chan
    assert t.channels[be]["project_id"] == "p1"


@pytest.mark.asyncio
async def test_unknown_channel_in_main_only_routes(orchestrator, transport):
    """Messages in untracked channels (not main, not project) are ignored."""
    await transport.simulate_user_message(
        channel_id="random_channel_xyz", user_id="u1", content="hi",
    )
    assert orchestrator.store.list_projects() == []
