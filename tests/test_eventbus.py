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
