"""Tests for asyncio EventBus."""

import asyncio

import pytest

from eventbus import Event, EventBus


@pytest.mark.asyncio
async def test_subscribe_and_emit():
    bus = EventBus()
    received = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("test", handler)
    await bus.emit(Event(type="test", payload={"x": 1}))

    assert len(received) == 1
    assert received[0].payload["x"] == 1


@pytest.mark.asyncio
async def test_unsubscribe():
    bus = EventBus()
    called = []

    async def handler(event: Event) -> None:
        called.append(event)

    bus.subscribe("test", handler)
    bus.unsubscribe("test", handler)
    await bus.emit(Event(type="test"))
    assert len(called) == 0


@pytest.mark.asyncio
async def test_handler_exception_isolated():
    bus = EventBus()
    good_called = []

    async def bad_handler(_: Event) -> None:
        raise RuntimeError("boom")

    async def good_handler(event: Event) -> None:
        good_called.append(event)

    bus.subscribe("test", bad_handler)
    bus.subscribe("test", good_handler)
    await bus.emit(Event(type="test"))

    assert len(good_called) == 1


@pytest.mark.asyncio
async def test_middleware_can_block_event():
    bus = EventBus()
    received = []

    async def blocker(event, handler):
        if getattr(event, "block", False):
            return
        await handler(event)

    bus.add_middleware(blocker)
    bus.subscribe("TestEvent", lambda e: received.append(e))

    class TestEvent:
        type = "TestEvent"

    class BlockEvent(TestEvent):
        block = True

    class PassEvent(TestEvent):
        block = False

    await bus.emit(BlockEvent())
    await bus.emit(PassEvent())
    await bus.shutdown()

    assert len(received) == 1
    assert received[0].block is False


@pytest.mark.asyncio
async def test_event_priority_order():
    bus = EventBus()
    results = []

    async def slow_handler(event):
        await asyncio.sleep(0.05)
        results.append(event.value)

    async def pass_through(event, handler):
        await handler(event)

    bus.add_middleware(pass_through)
    bus.subscribe("PriorityEvent", slow_handler)

    class PriorityEvent:
        def __init__(self, value, priority=0):
            self.value = value
            self.priority = priority

        def __lt__(self, other):
            return self.priority < other.priority

    await bus.emit(PriorityEvent("high", priority=1))
    await bus.emit(PriorityEvent("low", priority=10))
    await bus.shutdown()

    assert results == ["high", "low"]


@pytest.mark.asyncio
async def test_no_middleware_fast_path_dispatches_immediately():
    bus = EventBus()
    received = []

    class FastEvent:
        pass

    bus.subscribe("FastEvent", lambda e: received.append(e))
    await bus.emit(FastEvent())
    assert len(received) == 1
