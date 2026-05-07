"""Tests for the in-memory Discord bridge fake."""

from __future__ import annotations

import pytest

from mythos.discord_bridge import IncomingMessage, InMemoryDiscordBridge


@pytest.mark.asyncio
async def test_handler_invoked_on_user_message():
    bridge = InMemoryDiscordBridge(main_channel_id=100)
    received: list[IncomingMessage] = []

    async def handler(msg):
        received.append(msg)

    bridge.on_message(handler)
    await bridge.inject_user_message(channel_id=100, content="hi")
    assert len(received) == 1
    assert received[0].content == "hi"
    assert received[0].is_bot is False


@pytest.mark.asyncio
async def test_create_channel_and_send():
    bridge = InMemoryDiscordBridge(main_channel_id=100)
    cid = await bridge.create_channel("project-alpha")
    mid = await bridge.send(cid, "hello")
    assert mid.startswith("msg_")
    msgs = bridge.messages_in(cid)
    assert msgs[-1]["content"] == "hello"
    assert msgs[-1]["is_bot"] is True


@pytest.mark.asyncio
async def test_create_channel_failure_simulation():
    bridge = InMemoryDiscordBridge(main_channel_id=100)
    bridge.set_channel_create_failure(True)
    with pytest.raises(RuntimeError):
        await bridge.create_channel("doomed")


@pytest.mark.asyncio
async def test_handlers_chained_in_order():
    bridge = InMemoryDiscordBridge(main_channel_id=100)
    order: list[str] = []

    async def first(msg):
        order.append("first")

    async def second(msg):
        order.append("second")

    bridge.on_message(first)
    bridge.on_message(second)
    await bridge.inject_user_message(channel_id=100, content="x")
    assert order == ["first", "second"]
