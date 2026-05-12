from __future__ import annotations

import json

import pytest

from gateway.platforms.base import SendResult
from gateway.project_status import (
    format_completion_message,
    format_rollup_message,
    update_stream_status,
)
from hermes_cli.runstate import _runstate_path_project, write_project_runstate


class FakeAdapter:
    def __init__(self):
        self.edits = []
        self.sends = []
        self.role_sends = []

    async def edit_message(self, chat_id, message_id, content):
        self.edits.append((str(chat_id), str(message_id), content))
        return SendResult(success=True, message_id=str(message_id))

    async def send(self, chat_id, content, **kwargs):
        self.sends.append((str(chat_id), content, kwargs))
        return SendResult(success=True, message_id=f"send-{len(self.sends)}")

    async def send_for_role(self, role, chat_id, content, **kwargs):
        self.role_sends.append((role, str(chat_id), content, kwargs))
        return SendResult(success=True, message_id=f"role-{len(self.role_sends)}")


@pytest.fixture()
def projects_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("WORKFLOW_PROJECTS_ROOT", str(root))
    return root


def test_format_completion_message_lists_stream_roles():
    body = format_completion_message(
        "billing",
        {
            "frontend": {"status": "complete", "role": "frontend"},
            "tests": {"status": "complete", "role": "test"},
        },
    )

    assert "`billing` complete" in body
    assert "`frontend` (frontend)" in body
    assert "`tests` (test)" in body


@pytest.mark.asyncio
async def test_update_stream_status_announces_project_completion_once(projects_root):
    initial_status = {
        "frontend": {
            "status": "complete",
            "role": "frontend",
            "channel_id": "2001",
        },
        "backend": {
            "status": "working",
            "role": "backend",
            "channel_id": "2002",
        },
    }
    write_project_runstate(
        "scope-1",
        "billing",
        phase="streams-active",
        main_channel_id="1001",
        rollup_message_id="5001",
        stream_status=initial_status,
    )
    adapter = FakeAdapter()

    edited = await update_stream_status(
        runner=None,
        scope_id="scope-1",
        slug="billing",
        stream_name="backend",
        new_status="complete",
        detail="review approved",
        adapter=adapter,
    )

    assert edited is True
    assert len(adapter.edits) == 1
    assert "Status: ✅ all streams complete" in adapter.edits[0][2]
    assert len(adapter.role_sends) == 1
    role, channel_id, completion_body, kwargs = adapter.role_sends[0]
    assert role == "orchestrator"
    assert channel_id == "1001"
    assert kwargs == {}
    assert "✅ `billing` complete" in completion_body
    assert "• `frontend` (frontend)" in completion_body
    assert "• `backend` (backend)" in completion_body
    assert adapter.sends == []

    data = json.loads(_runstate_path_project("scope-1", "billing").read_text())
    assert data["phase"] == "done"
    assert data["completion_message_id"] == "role-1"
    assert data["completion_announced_at"]
    assert "completion_announcement_inflight_at" not in data
    assert data["stream_status"]["backend"]["status"] == "complete"

    await update_stream_status(
        runner=None,
        scope_id="scope-1",
        slug="billing",
        stream_name="backend",
        new_status="complete",
        detail="review approved again",
        adapter=adapter,
    )

    assert len(adapter.edits) == 2
    assert len(adapter.role_sends) == 1


@pytest.mark.asyncio
async def test_update_stream_status_does_not_announce_before_all_complete(projects_root):
    write_project_runstate(
        "scope-1",
        "billing",
        phase="streams-active",
        main_channel_id="1001",
        rollup_message_id="5001",
        stream_status={
            "frontend": {"status": "pending", "role": "frontend"},
            "backend": {"status": "working", "role": "backend"},
        },
    )
    adapter = FakeAdapter()

    await update_stream_status(
        runner=None,
        scope_id="scope-1",
        slug="billing",
        stream_name="frontend",
        new_status="complete",
        adapter=adapter,
    )

    assert adapter.role_sends == []
    data = json.loads(_runstate_path_project("scope-1", "billing").read_text())
    assert data["phase"] == "streams-active"
    assert "completion_message_id" not in data


@pytest.mark.asyncio
async def test_update_stream_status_can_announce_without_rollup_message(projects_root):
    write_project_runstate(
        "scope-1",
        "billing",
        phase="streams-active",
        main_channel_id="1001",
        stream_status={
            "frontend": {"status": "complete", "role": "frontend"},
            "backend": {"status": "working", "role": "backend"},
        },
    )
    adapter = FakeAdapter()

    edited = await update_stream_status(
        runner=None,
        scope_id="scope-1",
        slug="billing",
        stream_name="backend",
        new_status="complete",
        adapter=adapter,
    )

    assert edited is False
    assert adapter.edits == []
    assert len(adapter.role_sends) == 1


def test_format_rollup_message_complete_status():
    body = format_rollup_message(
        "billing",
        {
            "frontend": {"status": "complete", "role": "frontend"},
            "backend": {"status": "complete", "role": "backend"},
        },
    )

    assert "Status: ✅ all streams complete" in body
