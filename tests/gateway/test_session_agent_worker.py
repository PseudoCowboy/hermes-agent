"""Tests for gateway/session_agent_worker.py (P6).

The worker is the long-lived consumer of LongLivedSession.inbox. We
exercise it with a stub AIAgent so the tests don't spin up a real
provider stack, and a tiny FakeAdapter so the soft-fail path can post
its error message somewhere observable.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import gateway.session_agent_worker as worker_module
from gateway.session_agent_worker import session_agent_worker
from gateway.session_router import LongLivedSession


# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------


class FakeAgent:
    """Stand-in for run_agent.AIAgent.

    Records each ``run_conversation`` call.  Optionally raises on the
    first call to exercise the soft-fail path.
    """

    def __init__(self, *, raise_on_first: bool = False):
        self.calls: List[Dict[str, Any]] = []
        self._raise_on_first = raise_on_first
        self._call_count = 0

    def run_conversation(self, user_message, conversation_history=None, task_id=None, **kwargs):
        self._call_count += 1
        self.calls.append({
            "user_message": user_message,
            "history_len": len(conversation_history) if conversation_history else 0,
            "history": list(conversation_history or []),
            "task_id": task_id,
        })
        if self._raise_on_first and self._call_count == 1:
            raise RuntimeError("simulated agent failure")
        # Echo: append a synthetic assistant message to history so the
        # next turn can verify persistence.
        new_history = list(conversation_history or [])
        new_history.append({"role": "user", "content": user_message})
        new_history.append({"role": "assistant", "content": f"echo:{user_message}"})
        return {"conversation_history": new_history}


class FakeAdapter:
    def __init__(self):
        self.sent: List[tuple] = []

    async def send(self, chat_id, content, **kwargs):
        self.sent.append((chat_id, content))


class FakeRunner:
    def __init__(self):
        self.config = SimpleNamespace(model="x", max_iterations=10)


def _fake_event(text: str):
    return SimpleNamespace(text=text, source=SimpleNamespace(user_id="42"))


@pytest.fixture
def stub_construct(monkeypatch):
    """Replace _construct_agent_for_session with a factory that hands
    out a single FakeAgent instance, so tests can inspect its calls."""

    def _make(*, raise_on_first: bool = False):
        agent = FakeAgent(raise_on_first=raise_on_first)

        def _factory(session, system_prompt, runner):
            return agent

        monkeypatch.setattr(
            worker_module, "_construct_agent_for_session", _factory
        )
        return agent

    return _make


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_consumes_inbox_message_and_calls_agent(stub_construct):
    agent = stub_construct()
    session = LongLivedSession("session:1")
    session.persona = "orchestrator"
    session.main_channel_id = "111"
    runner = FakeRunner()
    adapter = FakeAdapter()

    task = asyncio.create_task(
        session_agent_worker(session, runner=runner, adapter=adapter)
    )
    try:
        await session.deliver_message(_fake_event("hello"))
        # Yield enough times for the worker to pull + run + persist.
        for _ in range(20):
            await asyncio.sleep(0)
            if agent.calls:
                break
        assert len(agent.calls) == 1
        assert agent.calls[0]["user_message"] == "hello"
        assert agent.calls[0]["task_id"] == "session:1"
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass


@pytest.mark.asyncio
async def test_worker_persists_history_across_turns(stub_construct):
    agent = stub_construct()
    session = LongLivedSession("session:2")
    session.persona = "orchestrator"
    session.main_channel_id = "222"
    runner = FakeRunner()
    adapter = FakeAdapter()

    task = asyncio.create_task(
        session_agent_worker(session, runner=runner, adapter=adapter)
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
        # Second call should see history from the first turn (user+assistant).
        assert agent.calls[1]["history_len"] >= 2
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass


@pytest.mark.asyncio
async def test_worker_soft_fails_on_agent_exception(stub_construct):
    agent = stub_construct(raise_on_first=True)
    session = LongLivedSession("session:3")
    session.persona = "orchestrator"
    session.main_channel_id = "333"
    runner = FakeRunner()
    adapter = FakeAdapter()

    task = asyncio.create_task(
        session_agent_worker(session, runner=runner, adapter=adapter)
    )
    try:
        await session.deliver_message(_fake_event("first"))
        for _ in range(50):
            await asyncio.sleep(0)
            if adapter.sent:
                break
        # Error message posted to main channel.
        assert any("error" in msg.lower() for _, msg in adapter.sent)
        # Worker must still be alive — send another message and verify it consumes.
        await session.deliver_message(_fake_event("second"))
        for _ in range(50):
            await asyncio.sleep(0)
            if len(agent.calls) >= 2:
                break
        assert len(agent.calls) == 2
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass


@pytest.mark.asyncio
async def test_worker_lazy_constructs_agent_on_first_turn(stub_construct, monkeypatch):
    constructed = []

    def _factory(session, system_prompt, runner):
        agent = FakeAgent()
        constructed.append(agent)
        return agent

    monkeypatch.setattr(worker_module, "_construct_agent_for_session", _factory)

    session = LongLivedSession("session:4")
    session.persona = "orchestrator"
    session.main_channel_id = "444"
    runner = FakeRunner()
    adapter = FakeAdapter()

    task = asyncio.create_task(
        session_agent_worker(session, runner=runner, adapter=adapter)
    )
    try:
        # No messages yet — agent should not have been constructed.
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
async def test_worker_skips_empty_text_events(stub_construct):
    agent = stub_construct()
    session = LongLivedSession("session:5")
    session.persona = "orchestrator"
    session.main_channel_id = "555"
    runner = FakeRunner()
    adapter = FakeAdapter()

    task = asyncio.create_task(
        session_agent_worker(session, runner=runner, adapter=adapter)
    )
    try:
        await session.deliver_message(_fake_event(""))
        for _ in range(20):
            await asyncio.sleep(0)
        # Empty text → no agent call.
        assert agent.calls == []

        # But a real text message after still works.
        await session.deliver_message(_fake_event("real message"))
        for _ in range(50):
            await asyncio.sleep(0)
            if agent.calls:
                break
        assert len(agent.calls) == 1
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass


@pytest.mark.asyncio
async def test_worker_cancellable_on_session_teardown(stub_construct):
    stub_construct()
    session = LongLivedSession("session:6")
    session.persona = "orchestrator"
    session.main_channel_id = "666"
    runner = FakeRunner()
    adapter = FakeAdapter()

    task = asyncio.create_task(
        session_agent_worker(session, runner=runner, adapter=adapter)
    )
    # Let the worker get into the await get_next() state.
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    # Cancellation should NOT raise out of the worker — it returns cleanly.
    try:
        await task
    except asyncio.CancelledError:
        # Acceptable: some Python versions propagate the CancelledError
        # even if the coroutine handles it, depending on timing.  The
        # important thing is the worker doesn't deadlock.
        pass


@pytest.mark.asyncio
async def test_worker_aborts_on_unknown_persona(stub_construct):
    """If load_persona_system_prompt raises, the worker posts an error
    and exits cleanly rather than crashing the whole asyncio task."""
    stub_construct()
    session = LongLivedSession("session:7")
    session.persona = "unknown_persona_xyz"
    session.main_channel_id = "777"
    runner = FakeRunner()
    adapter = FakeAdapter()

    # Run the worker — it should return on its own.
    await session_agent_worker(session, runner=runner, adapter=adapter)
    assert any("persona" in msg.lower() for _, msg in adapter.sent)
