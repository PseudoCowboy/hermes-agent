"""Unit tests for state transitions, slug, and approval gating."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from mythos.state import (ApprovalEvent, ChannelRef, DesignSpec, Project,
                          ProjectStore, slugify, new_project_id)
from mythos.roles import ChannelKind, ProjectStatus


def test_slugify():
    assert slugify("Build a Chrome translator extension!", max_len=64) == "build-a-chrome-translator-extension"
    assert slugify("Build a Chrome translator extension!") == "build-a-chrome-translator-exte"
    assert slugify("   ") == "project"
    assert slugify("foo bar baz", max_len=7) == "foo-bar"


def test_new_project_id_unique():
    a, _ = new_project_id()
    b, _ = new_project_id()
    assert a != b


def _project() -> Project:
    pid, sid = new_project_id()
    return Project(
        project_id=pid, short_id=sid, slug="foo",
        status=ProjectStatus.INTAKE_RECEIVED,
        created_by_discord_user_id=1,
        main_channel_id=10,
        original_request="something",
        workspace_path="/tmp/x",
    )


def test_is_approved_requires_matching_version():
    p = _project()
    p.design_specs.append(DesignSpec(version=1, content="v1", created_at=time.time()))
    p.approvals.append(ApprovalEvent(spec_version=1, approver_user_id=2,
                                     approved_at=time.time()))
    assert p.is_approved()

    # Add a new spec version → prior approval is now stale
    p.design_specs.append(DesignSpec(version=2, content="v2", created_at=time.time()))
    assert not p.is_approved()

    p.approvals.append(ApprovalEvent(spec_version=2, approver_user_id=2,
                                     approved_at=time.time()))
    assert p.is_approved()


def test_channel_lookup():
    p = _project()
    p.channels[ChannelKind.PLAN.value] = ChannelRef(
        kind=ChannelKind.PLAN, discord_channel_id=999, name="plan",
    )
    assert p.channel_for(ChannelKind.PLAN).discord_channel_id == 999
    assert p.channel_kind_for(999) == ChannelKind.PLAN
    assert p.channel_kind_for(123) is None


def test_store_round_trip(tmp_path: Path):
    store = ProjectStore(tmp_path / "state.json")
    p = _project()
    p.channels[ChannelKind.PLAN.value] = ChannelRef(
        kind=ChannelKind.PLAN, discord_channel_id=111, name="plan",
    )
    p.design_specs.append(DesignSpec(version=1, content="hello", created_at=1.0))
    store.create(p)

    fresh = ProjectStore(tmp_path / "state.json")
    got = fresh.get(p.project_id)
    assert got is not None
    assert got.slug == "foo"
    assert got.channels[ChannelKind.PLAN.value].discord_channel_id == 111
    assert got.design_specs[0].content == "hello"


def test_find_by_channel(tmp_path: Path):
    store = ProjectStore(tmp_path / "state.json")
    p = _project()
    p.channels[ChannelKind.PLAN.value] = ChannelRef(
        kind=ChannelKind.PLAN, discord_channel_id=555, name="plan",
    )
    store.create(p)
    fresh = ProjectStore(tmp_path / "state.json")
    assert fresh.find_by_channel(555) is not None
    assert fresh.find_by_channel(999) is None
