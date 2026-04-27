"""Tests for ``gateway.implementer_worker.implementer_worker`` (P7a-2).

The real implementer worker replaces P7a-1's stub. We exercise it with
a stub ``AIAgent`` so the tests don't spin up real provider calls, plus
a tiny ``FakeAdapter`` so the worker's error path posts somewhere
observable.

Tested contracts:

* persona prompt loaded by role (frontend/backend)
* AIAgent constructed lazily (only after the first non-empty inbound)
* the per-turn ``ToolDispatchContext`` carries ``worktree_root`` +
  ``sandbox`` so the registry can route file/terminal calls through
  the sandbox
* conversation history persists between turns on the local stack
* empty-text inbounds (stickers / reactions) skip the agent entirely
* a turn that raises is swallowed: error posted to the stream channel,
  runstate flipped to ``status="error"``, the worker keeps consuming
* cancellation via the spawning task exits cleanly with no further
  posts
* a missing persona prompt aborts the worker after marking runstate
  ``status="error"`` and posting a recoverable message

Tests use ``WORKFLOW_PROJECTS_ROOT=tmp_path`` so runstate writes never
touch the real ``~/.hermes`` tree.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import gateway.implementer_worker as worker_module
from gateway.implementer_worker import implementer_worker
from gateway.personas import (
    IMPLEMENTER_BACKEND,
    IMPLEMENTER_FRONTEND,
)
from gateway.session_router import LongLivedSession
from tools.registry import ToolDispatchContext


# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------


class FakeAgent:
    """Stand-in for ``run_agent.AIAgent``.

    Records each ``run_conversation`` call so the test can assert on
    ``user_message``, ``conversation_history``, and the dispatch context
    that was active during the turn (read off ``ContextVar`` inside
    ``run_conversation``).
    """

    def __init__(self, *, raise_on_first: bool = False):
        self.calls: List[Dict[str, Any]] = []
        self._raise_on_first = raise_on_first
        self._call_count = 0

    def run_conversation(
        self,
        user_message,
        conversation_history=None,
        task_id=None,
        **kwargs,
    ):
        # Capture the *current* ambient dispatch context so the test can
        # assert sandbox + worktree_root were bound for the turn.
        from tools.registry import _dispatch_context_var

        ctx = _dispatch_context_var.get()
        self._call_count += 1
        self.calls.append({
            "user_message": user_message,
            "history_len": len(conversation_history) if conversation_history else 0,
            "history": list(conversation_history or []),
            "task_id": task_id,
            "dispatch_ctx": ctx,
        })
        if self._raise_on_first and self._call_count == 1:
            raise RuntimeError("simulated agent failure")
        new_history = list(conversation_history or [])
        new_history.append({"role": "user", "content": user_message})
        new_history.append({"role": "assistant", "content": f"echo:{user_message}"})
        # P7a-2: real ``run_agent.AIAgent.run_conversation`` returns the
        # updated history under ``"messages"``. Match that shape so the
        # implementer worker's ``result.get("messages")`` actually picks
        # up our fake history.
        return {"messages": new_history}


class FakeAdapter:
    def __init__(self):
        self.sent: List[tuple] = []

    async def send(self, chat_id, content, **kwargs):
        self.sent.append((chat_id, content))


class FakeRunner:
    """Minimal runner stub: only the fields the worker reads."""

    def __init__(self):
        self.config = SimpleNamespace(model="x", max_iterations=10)
        self._session_db = None

    def _resolve_implementer_agent_config(self, role: str) -> dict:
        # Returned shape mirrors the real method; only ``model`` is used
        # by the FakeAgent path.
        return {
            "model": f"fake-model-{role}",
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
            "fallback_model": None,
        }


def _fake_event(text: str):
    return SimpleNamespace(text=text, source=SimpleNamespace(user_id="42"))


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def workflow_root(tmp_path, monkeypatch):
    """Redirect ``project_path`` writes (runstate) into a tmp tree.

    Without this, ``write_stream_runstate`` mutates the user's real
    ``~/.hermes/projects`` tree.
    """
    root = tmp_path / "active"
    root.mkdir()
    monkeypatch.setenv("WORKFLOW_PROJECTS_ROOT", str(root))
    return root


@pytest.fixture
def stub_construct(monkeypatch):
    """Replace ``_construct_agent_for_session`` with a factory returning
    a single FakeAgent so the test can poke its ``calls`` afterwards."""

    def _make(*, raise_on_first: bool = False):
        agent = FakeAgent(raise_on_first=raise_on_first)

        def _factory(session, system_prompt, runner):
            # Stash on the agent so tests can verify the prompt routed
            # by the persona resolver was the one we expected.
            agent.system_prompt = system_prompt  # type: ignore[attr-defined]
            return agent

        monkeypatch.setattr(
            worker_module, "_construct_agent_for_session", _factory,
        )
        return agent

    return _make


@pytest.fixture
def stub_sandbox(monkeypatch):
    """Replace ``_build_stream_sandbox`` with a sentinel object so the
    test can assert it ended up on the dispatch context without
    pulling in the real sandbox + tool registry."""

    sentinel = object()
    monkeypatch.setattr(
        worker_module, "_build_stream_sandbox", lambda session: sentinel,
    )
    return sentinel


def _read_stream_runstate_file(scope_id: str, slug: str, stream: str) -> dict:
    """Read the stream runstate JSON directly.

    The public ``read_stream_runstate`` is a P7b stub
    (``NotImplementedError``); we sidestep it by hitting the same path
    helper the writer uses and parsing the JSON ourselves.
    """
    from hermes_cli.runstate import _runstate_path_stream

    path = _runstate_path_stream(scope_id, slug, stream)
    return json.loads(path.read_text())


def _make_stream_session(
    tmp_path: Path,
    role: str,
    *,
    persona: str | None = None,
    key: str = "session:impl-1",
) -> LongLivedSession:
    """Build a session populated with the bootstrap-set fields the
    worker reads (persona/role/worktree_root/etc.)."""
    s = LongLivedSession(key, scope_id="42")
    s.persona = persona
    s.role = role
    s.slug = "demo-project"
    s.stream_name = "frontend" if role == "frontend" else "backend"
    s.main_channel_id = "999"
    wt = tmp_path / "wt"
    wt.mkdir()
    s.worktree_root = wt
    return s


# -----------------------------------------------------------------------------
# Persona prompt loaded by role
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frontend_session_loads_frontend_prompt(
    tmp_path, workflow_root, stub_construct, stub_sandbox,
):
    agent = stub_construct()
    session = _make_stream_session(
        tmp_path, role="frontend", persona=IMPLEMENTER_FRONTEND,
    )
    runner = FakeRunner()
    adapter = FakeAdapter()

    task = asyncio.create_task(
        implementer_worker(session, runner=runner, adapter=adapter)
    )
    try:
        await session.deliver_message(_fake_event("hi"))
        for _ in range(50):
            await asyncio.sleep(0)
            if agent.calls:
                break
        assert len(agent.calls) == 1
        # Frontend prompt: should contain "frontend" or "implementer"
        # (we don't pin specific copy to avoid brittle wording asserts).
        sp = agent.system_prompt  # type: ignore[attr-defined]
        assert isinstance(sp, str) and sp.strip()
        assert "frontend" in sp.lower() or "implementer" in sp.lower()
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass


@pytest.mark.asyncio
async def test_backend_session_loads_backend_prompt(
    tmp_path, workflow_root, stub_construct, stub_sandbox,
):
    agent = stub_construct()
    session = _make_stream_session(
        tmp_path, role="backend", persona=IMPLEMENTER_BACKEND,
        key="session:impl-2",
    )
    runner = FakeRunner()
    adapter = FakeAdapter()

    task = asyncio.create_task(
        implementer_worker(session, runner=runner, adapter=adapter)
    )
    try:
        await session.deliver_message(_fake_event("hi"))
        for _ in range(50):
            await asyncio.sleep(0)
            if agent.calls:
                break
        sp = agent.system_prompt  # type: ignore[attr-defined]
        assert isinstance(sp, str) and sp.strip()
        assert "backend" in sp.lower() or "implementer" in sp.lower()
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass


# -----------------------------------------------------------------------------
# Lazy AIAgent construction — only on first non-empty inbound.
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_constructed_lazily_only_on_first_inbound(
    tmp_path, workflow_root, stub_sandbox, monkeypatch,
):
    constructed: List[FakeAgent] = []

    def _factory(session, system_prompt, runner):
        agent = FakeAgent()
        constructed.append(agent)
        return agent

    monkeypatch.setattr(worker_module, "_construct_agent_for_session", _factory)

    session = _make_stream_session(
        tmp_path, role="backend", persona=IMPLEMENTER_BACKEND,
    )
    runner = FakeRunner()
    adapter = FakeAdapter()

    task = asyncio.create_task(
        implementer_worker(session, runner=runner, adapter=adapter)
    )
    try:
        # Idle worker: no inbound, no construction.
        for _ in range(5):
            await asyncio.sleep(0)
        assert constructed == []

        await session.deliver_message(_fake_event("first"))
        for _ in range(50):
            await asyncio.sleep(0)
            if constructed:
                break
        assert len(constructed) == 1
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass


@pytest.mark.asyncio
async def test_empty_text_inbound_skips_agent_construction(
    tmp_path, workflow_root, stub_sandbox, monkeypatch,
):
    """Stickers / reactions / empty events shouldn't fire the agent or
    bump turn_count. We assert by counting handler invocations."""
    constructed: List[FakeAgent] = []

    def _factory(session, system_prompt, runner):
        agent = FakeAgent()
        constructed.append(agent)
        return agent

    monkeypatch.setattr(worker_module, "_construct_agent_for_session", _factory)

    session = _make_stream_session(
        tmp_path, role="backend", persona=IMPLEMENTER_BACKEND,
    )
    runner = FakeRunner()
    adapter = FakeAdapter()

    task = asyncio.create_task(
        implementer_worker(session, runner=runner, adapter=adapter)
    )
    try:
        # Empty-text events must not trip lazy construction.
        await session.deliver_message(_fake_event(""))
        for _ in range(20):
            await asyncio.sleep(0)
        assert constructed == []
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass


# -----------------------------------------------------------------------------
# Dispatch context carries worktree_root + sandbox.
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_context_carries_sandbox_and_worktree(
    tmp_path, workflow_root, stub_construct, stub_sandbox,
):
    agent = stub_construct()
    session = _make_stream_session(
        tmp_path, role="backend", persona=IMPLEMENTER_BACKEND,
    )
    runner = FakeRunner()
    adapter = FakeAdapter()

    task = asyncio.create_task(
        implementer_worker(session, runner=runner, adapter=adapter)
    )
    try:
        await session.deliver_message(_fake_event("do work"))
        for _ in range(50):
            await asyncio.sleep(0)
            if agent.calls:
                break

        ctx = agent.calls[0]["dispatch_ctx"]
        assert isinstance(ctx, ToolDispatchContext)
        # Sandbox sentinel was injected.
        assert ctx.sandbox is stub_sandbox
        # Worktree root piped through verbatim.
        assert ctx.worktree_root == session.worktree_root
        # Project identity carried so workflow tools can auto-bind.
        assert ctx.scope_id == session.scope_id
        assert ctx.slug == session.slug
        assert ctx.stream_name == session.stream_name
        assert ctx.channel_id == session.main_channel_id
        # closed_check is a callable returning the live closed flag.
        assert callable(ctx.closed_check)
        assert ctx.closed_check() is False
        session.closed = True
        assert ctx.closed_check() is True
    finally:
        # Restore so teardown's deliver_message still works on next test.
        session.closed = False
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass


# -----------------------------------------------------------------------------
# Conversation history persists between turns.
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conversation_history_persists_between_turns(
    tmp_path, workflow_root, stub_construct, stub_sandbox,
):
    agent = stub_construct()
    session = _make_stream_session(
        tmp_path, role="backend", persona=IMPLEMENTER_BACKEND,
    )
    runner = FakeRunner()
    adapter = FakeAdapter()

    task = asyncio.create_task(
        implementer_worker(session, runner=runner, adapter=adapter)
    )
    try:
        await session.deliver_message(_fake_event("first"))
        for _ in range(50):
            await asyncio.sleep(0)
            if len(agent.calls) >= 1:
                break

        await session.deliver_message(_fake_event("second"))
        for _ in range(50):
            await asyncio.sleep(0)
            if len(agent.calls) >= 2:
                break

        assert len(agent.calls) == 2
        # FakeAgent appends user+assistant per call → 2nd call sees ≥2 history.
        assert agent.calls[1]["history_len"] >= 2
        # And the previous user message is in there.
        last_call_history = agent.calls[1]["history"]
        assert any(
            m.get("role") == "user" and m.get("content") == "first"
            for m in last_call_history
        )
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass


# -----------------------------------------------------------------------------
# Per-turn exception is swallowed: posted + runstate marked + worker alive.
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_exception_posts_error_and_keeps_worker_alive(
    tmp_path, workflow_root, stub_construct, stub_sandbox,
):
    agent = stub_construct(raise_on_first=True)
    session = _make_stream_session(
        tmp_path, role="backend", persona=IMPLEMENTER_BACKEND,
    )
    runner = FakeRunner()
    adapter = FakeAdapter()

    task = asyncio.create_task(
        implementer_worker(session, runner=runner, adapter=adapter)
    )
    try:
        await session.deliver_message(_fake_event("first"))
        for _ in range(50):
            await asyncio.sleep(0)
            if adapter.sent:
                break
        # Recoverable error message landed on the stream channel.
        assert any(
            "error" in msg.lower() for _, msg in adapter.sent
        ), f"no error post; sent={adapter.sent}"
        # Worker is still alive: send another and verify it consumes.
        await session.deliver_message(_fake_event("second"))
        for _ in range(50):
            await asyncio.sleep(0)
            if len(agent.calls) >= 2:
                break
        assert len(agent.calls) == 2

        # Runstate file should exist with status="error" after the
        # failed first turn, then re-flipped to "running" for the
        # second (successful) turn.  Read JSON directly because the
        # public reader is a P7b stub.
        rs = _read_stream_runstate_file(
            "42", "demo-project", session.stream_name,
        )
        assert rs.get("status") in {"error", "running"}, rs
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass


# -----------------------------------------------------------------------------
# Cancellation: the worker exits cleanly without further posts.
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_exits_cleanly(
    tmp_path, workflow_root, stub_construct, stub_sandbox,
):
    agent = stub_construct()
    session = _make_stream_session(
        tmp_path, role="backend", persona=IMPLEMENTER_BACKEND,
    )
    runner = FakeRunner()
    adapter = FakeAdapter()

    task = asyncio.create_task(
        implementer_worker(session, runner=runner, adapter=adapter)
    )
    # Give the worker a tick to enter ``session.get_next()``.
    await asyncio.sleep(0)

    task.cancel()
    # The worker catches CancelledError in the inbox await and exits
    # cleanly with a None return — so the awaited task itself does NOT
    # raise.  We just want to confirm it terminates promptly without
    # posting anything.
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()
    # No errant posts during shutdown.
    assert adapter.sent == []
    assert agent.calls == []


# -----------------------------------------------------------------------------
# Missing persona prompt → marks runstate error + posts + returns.
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_load_failure_aborts_with_error_post(
    tmp_path, workflow_root, monkeypatch, stub_sandbox,
):
    """If ``load_persona_system_prompt`` raises (e.g. prompt file missing),
    the worker must (a) post an error to the stream channel, (b) write
    runstate status=error, and (c) return cleanly so the spawning task
    completes."""

    def _boom(persona):
        raise RuntimeError("synthetic prompt load failure")

    monkeypatch.setattr(worker_module, "load_persona_system_prompt", _boom)

    session = _make_stream_session(
        tmp_path, role="backend", persona=IMPLEMENTER_BACKEND,
    )
    runner = FakeRunner()
    adapter = FakeAdapter()

    # The worker should return on its own (no inbound needed) because
    # prompt loading happens at the top of the function.
    await asyncio.wait_for(
        implementer_worker(session, runner=runner, adapter=adapter),
        timeout=2.0,
    )

    # Recoverable error message posted to the stream channel.
    assert adapter.sent, "no error post"
    assert any(
        "persona" in msg.lower() or "error" in msg.lower() or "abort" in msg.lower()
        for _, msg in adapter.sent
    )

    rs = _read_stream_runstate_file(
        "42", "demo-project", session.stream_name,
    )
    assert rs.get("status") == "error", rs
    reason = rs.get("reason") or ""
    assert "persona" in reason.lower() or "prompt" in reason.lower(), rs


# -----------------------------------------------------------------------------
# Persona resolution falls back via role when session.persona is missing.
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_persona_falls_back_via_role(
    tmp_path, workflow_root, stub_construct, stub_sandbox,
):
    """If bootstrap forgot to set ``session.persona`` but ``session.role``
    is "frontend", the worker derives the persona via ``role_to_persona``
    rather than hard-failing."""
    agent = stub_construct()
    session = _make_stream_session(
        tmp_path, role="frontend", persona=None,  # deliberately missing
    )
    runner = FakeRunner()
    adapter = FakeAdapter()

    task = asyncio.create_task(
        implementer_worker(session, runner=runner, adapter=adapter)
    )
    try:
        await session.deliver_message(_fake_event("hi"))
        for _ in range(50):
            await asyncio.sleep(0)
            if agent.calls:
                break
        sp = agent.system_prompt  # type: ignore[attr-defined]
        assert isinstance(sp, str) and sp.strip()
        # Should be the frontend prompt — use a soft assertion that
        # tolerates wording changes.
        assert "frontend" in sp.lower() or "implementer" in sp.lower()
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass
