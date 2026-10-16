from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    name: str
    data: str


class EventBus:
    """In-process pub/sub feeding SSE clients. Slow clients lose events rather than block."""

    def __init__(self, maxsize: int = 500) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._maxsize = maxsize

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(self._maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    def publish(self, name: str, data: str) -> None:
        """Must run on the event loop thread; use publish_threadsafe from anywhere else."""
        event = Event(name, data)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def publish_threadsafe(self, name: str, data: str) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self.publish(name, data)
        else:
            loop.call_soon_threadsafe(self.publish, name, data)


def format_sse(event: Event) -> str:
    lines = event.data.splitlines() or [""]
    return f"event: {event.name}\n" + "".join(f"data: {line}\n" for line in lines) + "\n"
