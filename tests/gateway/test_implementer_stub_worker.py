"""Tests for gateway.implementer_worker (P7a-1 stub).

The stub worker:
* Posts a startup banner into the stream channel on first run.
* Replies with a placeholder for each non-empty inbound message.
* Bumps ``turn_count`` and writes the stream runstate per turn.
* Skips empty-text events (stickers etc.) without bumping the count.
* Honors cancellation cleanly.
* Never crashes the worker on a per-turn error.

We use minimal fakes for the adapter (``send`` only) and a real
``LongLivedSession`` so we exercise the actual inbox + closed-flag
plumbing.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

from gateway.implementer_worker import (
    REPLY_MESSAGE,
    STARTUP_MESSAGE,
    implementer_stub_worker,
)
from gateway.session_router import InboundMessage, LongLivedSession


# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------


class FakeAdapter:
    def __init__(self, *, fail_send: bool = False):
        self.sent: List[tuple] = []
        self.fail_send = fail_send

    async def send(self, chat_id, content, **kwargs):
        if self.fail_send:
            raise RuntimeError("simulated send failure")
        self.sent.append((str(chat_id), content))


@pytest.fixture()
def projects_root(tmp_path, monkeypatch):
    root = tmp_path / "groups" / "shared_project" / "active"
    root.mkdir(parents=True)
    monkeypatch.setenv("WORKFLOW_PROJECTS_ROOT", str(root))
    monkeypatch.chdir(tmp_path)
    return root


def _make_session(channel_id="500"):
    s = LongLivedSession("scope-1:billing:frontend")
    s.scope_id = "scope-1"
    s.slug = "billing"
    s.stream_name = "frontend"
    s.main_channel_id = channel_id
    s.persona = "implementer"
    s.role = "backend"
    s.worktree_root = Path("/tmp/wt")
    return s


def _make_event(text, user_id="42"):
    src = SimpleNamespace(user_id=user_id)
    return SimpleNamespace(text=text, source=src)


# -----------------------------------------------------------------------------
# Startup banner
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_posts_startup_banner_on_first_run(projects_root):
    session = _make_session(channel_id="500")
    adapter = FakeAdapter()
    runner = SimpleNamespace()

    task = asyncio.create_task(
        implementer_stub_worker(session, runner=runner, adapter=adapter)
    )
    # Give the worker a tick to post the banner before it blocks on
    # the empty inbox.
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, BaseException):
        pass

    assert len(adapter.sent) == 1
    chat_id, msg = adapter.sent[0]
    assert chat_id == "500"
    assert "frontend" in msg
    assert msg == STARTUP_MESSAGE.format(stream_name="frontend")


# -----------------------------------------------------------------------------
# Reply to inbox messages
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_replies_to_inbox_messages_with_placeholder(projects_root):
    session = _make_session()
    adapter = FakeAdapter()
    runner = SimpleNamespace()

    task = asyncio.create_task(
        implementer_stub_worker(session, runner=runner, adapter=adapter)
    )
    await asyncio.sleep(0.02)  # let banner post
    await session._inbox.put(
        InboundMessage(kind="message", event=_make_event("hi there"))
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, BaseException):
        pass

    # Banner + one reply.
    assert len(adapter.sent) == 2
    assert adapter.sent[1][1] == REPLY_MESSAGE


# -----------------------------------------------------------------------------
# turn_count + runstate update
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_writes_stream_runstate_with_turn_count(projects_root):
    session = _make_session()
    adapter = FakeAdapter()
    runner = SimpleNamespace()

    task = asyncio.create_task(
        implementer_stub_worker(session, runner=runner, adapter=adapter)
    )
    await asyncio.sleep(0.02)
    await session._inbox.put(
        InboundMessage(kind="message", event=_make_event("first"))
    )
    await session._inbox.put(
        InboundMessage(kind="message", event=_make_event("second"))
    )
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, BaseException):
        pass

    from hermes_cli.runstate import _runstate_path_stream

    rs_path = _runstate_path_stream("scope-1", "billing", "frontend")
    assert rs_path.is_file(), f"runstate not written at {rs_path}"
    data = json.loads(rs_path.read_text())
    assert data["status"] == "running"
    assert data["turn_count"] == 2


# -----------------------------------------------------------------------------
# Empty-text events are skipped without bumping turn_count or sending
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_skips_empty_text_events(projects_root):
    session = _make_session()
    adapter = FakeAdapter()
    runner = SimpleNamespace()

    task = asyncio.create_task(
        implementer_stub_worker(session, runner=runner, adapter=adapter)
    )
    await asyncio.sleep(0.02)
    await session._inbox.put(
        InboundMessage(kind="message", event=_make_event(""))
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, BaseException):
        pass

    # Only the banner should have been sent — empty events skipped.
    assert len(adapter.sent) == 1
    # No runstate written either.
    from hermes_cli.runstate import _runstate_path_stream

    rs_path = _runstate_path_stream("scope-1", "billing", "frontend")
    assert not rs_path.exists()


# -----------------------------------------------------------------------------
# Cancellation
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_worker_honors_cancellation(projects_root):
    session = _make_session()
    adapter = FakeAdapter()
    runner = SimpleNamespace()

    task = asyncio.create_task(
        implementer_stub_worker(session, runner=runner, adapter=adapter)
    )
    await asyncio.sleep(0.02)  # banner
    task.cancel()
    # The worker swallows CancelledError and returns cleanly — that's
    # the lifecycle contract.  Awaiting may produce None or raise
    # CancelledError depending on whether cancellation propagated
    # through asyncio.Queue.get() before the except handler fired.
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.done()


# -----------------------------------------------------------------------------
# Closed session: don't post anything, don't crash
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_skips_post_when_session_closed(projects_root):
    session = _make_session()
    session.closed = True  # closed before worker starts
    adapter = FakeAdapter()
    runner = SimpleNamespace()

    task = asyncio.create_task(
        implementer_stub_worker(session, runner=runner, adapter=adapter)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, BaseException):
        pass

    # Banner suppressed because session.closed is True.
    assert adapter.sent == []


# -----------------------------------------------------------------------------
# Adapter send failure does NOT kill the worker
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_survives_adapter_send_failure(projects_root):
    session = _make_session()
    adapter = FakeAdapter(fail_send=True)
    runner = SimpleNamespace()

    task = asyncio.create_task(
        implementer_stub_worker(session, runner=runner, adapter=adapter)
    )
    await asyncio.sleep(0.02)  # banner attempt fails silently
    await session._inbox.put(
        InboundMessage(kind="message", event=_make_event("hi"))
    )
    await asyncio.sleep(0.05)
    # Worker should still be alive (didn't crash on send failure).
    assert not task.done()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, BaseException):
        pass
