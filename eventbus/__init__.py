"""Asyncio EventBus for inter-module communication.

Simple pub/sub: modules subscribe to event types and emit events.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine


@dataclass(frozen=True)
class Event:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)


EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = {}

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        if event_type in self._subscribers:
            self._subscribers[event_type] = [h for h in self._subscribers[event_type] if h is not handler]

    async def emit(self, event: Event | Any) -> None:
        event_type = getattr(event, "type", None)
        if event_type is None:
            event_type = event.__class__.__name__
        handlers = self._subscribers.get(event_type, [])
        if not handlers:
            return
        await asyncio.gather(
            *(self._invoke(h, event) for h in handlers),
            return_exceptions=True,
        )

    async def _invoke(self, handler: EventHandler, event: Event) -> None:
        try:
            await handler(event)
        except Exception as exc:
            # Errors in event handlers should not crash the bus.
            print(f"[eventbus] handler error for {event.type}: {exc}")
