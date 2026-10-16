from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.templating import templates


async def get_session(request: Request) -> AsyncIterator[Session]:
    # async dependency: the session lives on the event-loop thread, like all other DB work
    with request.app.state.session_factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def render(request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200) -> Response:
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code)


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def done(request: Request, url: str) -> Response:
    """Response for a fire-and-forget action: 204 for htmx, a redirect for plain forms."""
    if is_htmx(request):
        return Response(status_code=204)
    return RedirectResponse(url, status_code=303)


def get_or_404(session: Session, model: type, object_id: int):
    obj = session.get(model, object_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Not found")
    return obj


def spawn(app: FastAPI, coro: Coroutine) -> asyncio.Task:
    task = asyncio.create_task(coro)
    app.state.background.add(task)
    task.add_done_callback(app.state.background.discard)
    return task
