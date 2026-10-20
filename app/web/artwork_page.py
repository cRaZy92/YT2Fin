from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse

from app import artwork, organizer, services
from app.models import Mapping, MappingType
from app.web.deps import SessionDep, get_or_404, spawn

router = APIRouter()

SOURCE_LABELS = {
    artwork.CHANNEL: "from the channel", artwork.CUSTOM: "uploaded", artwork.EPISODE: "first episode's thumbnail",
    "other": "placed by someone else", None: "none yet",
}


def artwork_context(media_root: Path, mapping: Mapping) -> list[dict]:
    """One tile per image kind for the mapping page; empty for music videos."""
    if mapping.type != MappingType.SHOW:
        return []
    folder = organizer.mapping_dir(media_root, mapping)
    tiles = []
    for kind, label in artwork.KINDS.items():
        path = artwork.current_file(folder, mapping, kind) if folder.is_dir() else None
        source = artwork.source(folder, mapping, kind) if path else None
        tiles.append({
            "kind": kind, "label": label, "source": SOURCE_LABELS[source],
            "version": path.stat().st_mtime_ns if path else None, "custom": source == artwork.CUSTOM,
        })
    return tiles


def _show(session, mapping_id: int) -> Mapping:
    mapping = get_or_404(session, Mapping, mapping_id)
    if mapping.type != MappingType.SHOW:
        raise HTTPException(status_code=400, detail="Only shows have artwork")
    return mapping


def _kind(kind: str) -> str:
    if kind not in artwork.KINDS:
        raise HTTPException(status_code=404, detail="Unknown image")
    return kind


@router.get("/mappings/{mapping_id}/artwork/{kind}")
async def artwork_image(request: Request, mapping_id: int, kind: str, session: SessionDep):
    mapping = _show(session, mapping_id)
    path = artwork.current_file(organizer.mapping_dir(request.app.state.config.media_root, mapping), mapping, _kind(kind))
    if path is None:
        raise HTTPException(status_code=404, detail="No image")
    return FileResponse(path)


@router.post("/mappings/{mapping_id}/artwork/refresh")
async def refresh_artwork(request: Request, mapping_id: int, session: SessionDep):
    mapping = _show(session, mapping_id)
    if not mapping.playlist_url:
        return PlainTextResponse("A manual list has no channel to take images from", status_code=400)
    if request.app.state.worker.cooldown_until is not None:
        return PlainTextResponse("Paused while YouTube rate-limits us", status_code=409)
    spawn(request.app, request.app.state.syncer.refresh_show_info(mapping_id))
    return RedirectResponse(f"/mappings/{mapping_id}", status_code=303)


@router.post("/mappings/{mapping_id}/artwork/{kind}")
async def upload_artwork(request: Request, mapping_id: int, kind: str, session: SessionDep,
                         file: Annotated[UploadFile, File()]):
    mapping = _show(session, mapping_id)
    data = await file.read(artwork.MAX_BYTES + 1)
    if len(data) > artwork.MAX_BYTES:
        return PlainTextResponse("The image is larger than 25 MB", status_code=400)
    folder = organizer.mapping_dir(request.app.state.config.media_root, mapping)
    try:
        artwork.install(folder, mapping, _kind(kind), data, artwork.CUSTOM)
    except artwork.ArtworkError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    session.commit()
    return RedirectResponse(f"/mappings/{mapping_id}", status_code=303)


@router.post("/mappings/{mapping_id}/artwork/{kind}/reset")
async def reset_artwork(request: Request, mapping_id: int, kind: str, session: SessionDep):
    """Drop the uploaded image; the channel's image (or the episode-thumbnail poster) takes over again."""
    mapping = _show(session, mapping_id)
    media_root = request.app.state.config.media_root
    artwork.remove(organizer.mapping_dir(media_root, mapping), mapping, _kind(kind))
    session.commit()
    organizer.apply(session, media_root, mapping, services.max_len(session))
    if mapping.playlist_url and request.app.state.worker.cooldown_until is None:
        spawn(request.app, request.app.state.syncer.refresh_show_info(mapping_id, force=False))
    return RedirectResponse(f"/mappings/{mapping_id}", status_code=303)
