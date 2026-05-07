"""Tests for the in-memory store and per-project isolation invariants."""

from __future__ import annotations

import pytest

from mythos.models import AgentRole, ProjectState, WorkstreamType
from mythos.store import ProjectStore


@pytest.mark.asyncio
async def test_distinct_project_ids_and_slugs():
    store = ProjectStore()
    p1 = await store.create_project(
        display_name="Translator Extension",
        requester="alice",
        request_text="x",
        source_message_id="m1",
    )
    p2 = await store.create_project(
        display_name="Translator Extension",
        requester="bob",
        request_text="y",
        source_message_id="m2",
    )
    assert p1.id != p2.id
    assert p1.project_channel_name == "translator-extension"
    assert p2.project_channel_name == "translator-extension-2"


@pytest.mark.asyncio
async def test_channel_routing_is_isolated_per_project():
    store = ProjectStore()
    p1 = await store.create_project(
        display_name="Alpha", requester="u", request_text="x", source_message_id="m1"
    )
    p2 = await store.create_project(
        display_name="Beta", requester="u", request_text="y", source_message_id="m2"
    )
    await store.attach_project_channel(p1.id, 4001)
    await store.attach_project_channel(p2.id, 4002)
    assert store.project_for_channel(4001).id == p1.id
    assert store.project_for_channel(4002).id == p2.id
    assert store.project_for_channel(9999) is None


@pytest.mark.asyncio
async def test_workstream_channel_routes_to_workstream_only():
    store = ProjectStore()
    p = await store.create_project(
        display_name="Gamma", requester="u", request_text="z", source_message_id="m"
    )
    await store.attach_project_channel(p.id, 5000)
    ws = await store.add_workstream(p.id, WorkstreamType.FRONTEND, AgentRole.APOLLO, "scope")
    await store.attach_workstream_channel(p.id, ws.id, 5001)

    assert store.workstream_for_channel(5001) is not None
    assert store.workstream_for_channel(5000) is None  # project channel != workstream channel
    pair = store.workstream_for_channel(5001)
    assert pair is not None
    proj, found_ws = pair
    assert proj.id == p.id
    assert found_ws.id == ws.id


@pytest.mark.asyncio
async def test_spec_versioning_and_review_status():
    store = ProjectStore()
    p = await store.create_project(
        display_name="Delta", requester="u", request_text="x", source_message_id="m"
    )
    s1 = await store.add_spec(p.id, "spec v1")
    s2 = await store.add_spec(p.id, "spec v2")
    assert s1.version == 1 and s2.version == 2
    await store.add_review(p.id, 1, "needs work", "request_changes", "major")
    await store.add_review(p.id, 2, "lgtm", "accept", "info")
    assert p.specs[0].review_status == "needs_changes"
    assert p.specs[1].review_status == "acceptable"


@pytest.mark.asyncio
async def test_state_persistence_appends_jsonl(tmp_path):
    persistence = tmp_path / "events.jsonl"
    store = ProjectStore(persistence_path=persistence)
    p = await store.create_project(
        display_name="Persist", requester="u", request_text="x", source_message_id="m"
    )
    await store.attach_project_channel(p.id, 7000)
    await store.transition(p.id, ProjectState.DRAFTING)
    contents = persistence.read_text().strip().splitlines()
    assert any("project_created" in line for line in contents)
    assert any("project_state_changed" in line for line in contents)
