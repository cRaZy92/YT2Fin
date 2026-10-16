from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.events import Event, EventBus, format_sse

router = APIRouter()
KEEPALIVE_SECONDS = 15
POLL_SECONDS = 1.0


async def event_stream(bus: EventBus, queue: asyncio.Queue[Event],
                       is_disconnected: Callable[[], Awaitable[bool]],
                       is_stopping: Callable[[], bool] = lambda: False) -> AsyncIterator[str]:
    """Ends when the client leaves or the server is shutting down (open tabs must not block restarts)."""
    loop = asyncio.get_running_loop()
    try:
        yield "retry: 3000\n\n"
        last_sent = loop.time()
        while not is_stopping():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=POLL_SECONDS)
            except TimeoutError:
                if is_stopping() or await is_disconnected():
                    break
                if loop.time() - last_sent >= KEEPALIVE_SECONDS:
                    last_sent = loop.time()
                    yield ": keepalive\n\n"
                continue
            last_sent = loop.time()
            yield format_sse(event)
    finally:
        bus.unsubscribe(queue)


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    bus: EventBus = request.app.state.bus
    queue = bus.subscribe()
    state = request.app.state

    def is_stopping() -> bool:
        return state.stopping or (state.server is not None and state.server.should_exit)

    return StreamingResponse(
        event_stream(bus, queue, request.is_disconnected, is_stopping),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
