"""Asyncio EventBus for inter-module communication.

Simple pub/sub: modules subscribe to event types and emit events.
Supports middleware chains and priority-queued dispatch when middleware
is registered, while keeping the original direct-dispatch fast path
available when no middleware is present.
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
Middleware = Callable[[Any, EventHandler], Coroutine[Any, Any, None]]


@dataclass(order=True)
class _QueuedEvent:
    priority: int
    seq: int
    event: Any = field(compare=False)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = {}
        self._middleware: list[Middleware] = []
        self._queue: asyncio.PriorityQueue[_QueuedEvent] = asyncio.PriorityQueue()
        self._worker: asyncio.Task | None = None
        self._seq: int = 0
        self._shutdown_event: asyncio.Event = asyncio.Event()

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        if event_type in self._subscribers:
            self._subscribers[event_type] = [h for h in self._subscribers[event_type] if h is not handler]

    def add_middleware(self, middleware: Middleware) -> None:
        self._middleware.append(middleware)

    def _handlers_for(self, event: Any) -> list[EventHandler]:
        event_type = getattr(event, "type", None)
        if event_type is None:
            event_type = event.__class__.__name__
        return self._subscribers.get(event_type, [])

    async def emit(self, event: Event | Any) -> None:
        handlers = self._handlers_for(event)
        if not handlers:
            return

        if not self._middleware:
            # Fast path: direct dispatch when no middleware is registered.
            await asyncio.gather(
                *(self._invoke(h, event) for h in handlers),
                return_exceptions=True,
            )
            return

        # Middleware path: enqueue with priority and let the worker dispatch.
        if self._worker is None:
            self._worker = asyncio.create_task(self._process_loop())

        priority = getattr(event, "priority", 0)
        seq = self._seq
        self._seq += 1
        self._queue.put_nowait(_QueuedEvent(priority, seq, event))

    async def shutdown(self) -> None:
        """Signal the background worker to stop and drain the queue."""
        if self._worker is None or self._worker.done():
            return
        self._shutdown_event.set()
        try:
            await asyncio.wait_for(self._worker, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        finally:
            if not self._worker.done():
                self._worker.cancel()
                try:
                    await self._worker
                except asyncio.CancelledError:
                    pass

    async def _process_loop(self) -> None:
        while not (self._shutdown_event.is_set() and self._queue.empty()):
            try:
                queued = await asyncio.wait_for(self._queue.get(), timeout=0.05)
            except asyncio.TimeoutError:
                continue
            await self._dispatch_with_middleware(queued.event)

    async def _dispatch_with_middleware(self, event: Any) -> None:
        handlers = self._handlers_for(event)
        if not handlers:
            return
        await asyncio.gather(
            *(self._invoke_with_middleware(event, h) for h in handlers),
            return_exceptions=True,
        )

    async def _invoke_with_middleware(self, event: Any, handler: EventHandler) -> None:
        await self._run_middleware(event, handler, 0)

    async def _run_middleware(self, event: Any, handler: EventHandler, index: int) -> None:
        if index >= len(self._middleware):
            await self._invoke(handler, event)
            return
        mw = self._middleware[index]

        async def next_step(e: Any) -> None:
            await self._run_middleware(e, handler, index + 1)

        await mw(event, next_step)

    async def _invoke(self, handler: EventHandler, event: Any) -> None:
        try:
            if asyncio.iscoroutinefunction(handler):
                await handler(event)
            else:
                handler(event)
        except Exception as exc:
            event_label = getattr(event, "type", type(event).__name__)
            print(f"[eventbus] handler error for {event_label}: {exc}")
