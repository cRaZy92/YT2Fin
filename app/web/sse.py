from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.events import Event, EventBus, format_sse

router = APIRouter()
KEEPALIVE_SECONDS = 15


async def event_stream(bus: EventBus, queue: asyncio.Queue[Event],
                       is_disconnected: Callable[[], Awaitable[bool]]) -> AsyncIterator[str]:
    try:
        yield "retry: 3000\n\n"
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except TimeoutError:
                if await is_disconnected():
                    break
                yield ": keepalive\n\n"
                continue
            yield format_sse(event)
    finally:
        bus.unsubscribe(queue)


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    bus: EventBus = request.app.state.bus
    queue = bus.subscribe()
    return StreamingResponse(
        event_stream(bus, queue, request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
