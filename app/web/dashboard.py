from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from app import services
from app.models import Mapping
from app.web.deps import SessionDep, done, get_or_404, render

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@router.get("/")
async def index(request: Request, session: SessionDep):
    mappings = session.scalars(select(Mapping).order_by(Mapping.name)).all()
    return render(request, "dashboard.html", {
        "mappings": mappings, "stats": services.mapping_stats(session), "empty": services.EMPTY_STATS,
        "syncing": request.app.state.syncer.running,
    })


@router.get("/mappings/{mapping_id}/card")
async def mapping_card(request: Request, mapping_id: int, session: SessionDep):
    mapping = get_or_404(session, Mapping, mapping_id)
    return render(request, "partials/mapping_card.html", {
        "m": mapping, "stats": services.mapping_stats(session), "empty": services.EMPTY_STATS,
        "syncing": request.app.state.syncer.running,
    })


@router.get("/mappings/{mapping_id}/stats")
async def mapping_stats(request: Request, mapping_id: int, session: SessionDep):
    mapping = get_or_404(session, Mapping, mapping_id)
    stats = services.mapping_stats(session).get(mapping_id, services.EMPTY_STATS)
    return render(request, "partials/mapping_stats.html", {"mapping": mapping, "s": stats})


@router.get("/activity")
async def activity(request: Request):
    return render(request, "partials/activity.html", {"a": request.app.state.worker.snapshot()})


@router.post("/queue/pause")
async def pause(request: Request):
    request.app.state.worker.pause()
    return done(request, "/")


@router.post("/queue/resume")
async def resume(request: Request):
    request.app.state.worker.resume()
    return done(request, "/")


@router.get("/logs")
async def logs(request: Request):
    return render(request, "logs.html", {"lines": list(request.app.state.log_buffer.lines)})
