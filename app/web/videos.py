from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from app import naming, organizer, services
from app import settings as settings_mod
from app.models import Mapping, MappingType, Video
from app.services import ServiceError
from app.web.deps import SessionDep, get_or_404, render
from app.ytdlp_client import DownloadFailed, is_channel_url

router = APIRouter()


def list_context(session, mapping: Mapping) -> dict:
    values = settings_mod.resolve(session, mapping)
    videos = organizer.ordered_videos(mapping)
    stems = organizer.target_stems(mapping, values["max_filename_length"])
    numbers = naming.season_episode(organizer.naming_items(mapping)) if mapping.type == MappingType.SHOW else {}
    previews = {}
    for video in videos:
        if video.id in stems:
            ext = Path(video.file_path).suffix if video.file_path else f".{values['container']}"
            previews[video.id] = stems[video.id] + ext
    return {
        "mapping": mapping,
        "videos": videos,
        "previews": previews,
        "codes": {vid: naming.episode_code(*n) for vid, n in numbers.items()},
        "season_starts": {vid: n[0] for vid, n in numbers.items() if n[1] == 1},
        "breaks": {b.video_id for b in mapping.season_breaks},
        "first_id": videos[0].id if videos else None,
    }


def preview_name(session, video: Video, *, title: str | None = None, artist: str | None = None,
                 song: str | None = None) -> str:
    """The file name the video would get with the given (unsaved) title or artist/song."""
    mapping = video.mapping
    values = settings_mod.resolve(session, mapping)
    items = []
    for item in organizer.naming_items(mapping):
        if item.video_id == video.id:
            item = replace(
                item,
                title=item.title if title is None else (title.strip() or video.yt_title or video.youtube_id),
                artist=item.artist if artist is None else artist,
                song=item.song if song is None else song,
            )
        items.append(item)
    stems = naming.compute_stems(mapping.type, mapping.show_title or mapping.name, items, values["max_filename_length"])
    if video.id not in stems:
        return "not numbered (unavailable)"
    ext = Path(video.file_path).suffix if video.file_path else f".{values['container']}"
    return stems[video.id] + ext


def _list(request: Request, session, mapping: Mapping, *, error: str | None = None,
          notice: str | None = None, status_code: int = 200):
    context = list_context(session, mapping) | {"error": error, "notice": notice}
    return render(request, "partials/video_list.html", context, status_code=status_code)


def _media_root(request: Request) -> Path:
    return request.app.state.config.media_root


@router.get("/mappings/{mapping_id}")
async def detail(request: Request, mapping_id: int, session: SessionDep):
    mapping = get_or_404(session, Mapping, mapping_id)
    context = list_context(session, mapping) | {
        "syncing": mapping_id in request.app.state.syncer.running,
        "is_channel": bool(mapping.playlist_url) and is_channel_url(mapping.playlist_url),
        "s": services.mapping_stats(session).get(mapping_id, services.EMPTY_STATS),
    }
    return render(request, "mapping_detail.html", context)


@router.get("/mappings/{mapping_id}/list")
async def video_list(request: Request, mapping_id: int, session: SessionDep):
    return _list(request, session, get_or_404(session, Mapping, mapping_id))


@router.post("/mappings/{mapping_id}/reorder")
async def reorder(request: Request, mapping_id: int, session: SessionDep):
    mapping = get_or_404(session, Mapping, mapping_id)
    form = await request.form()
    try:
        ids = [int(part) for part in str(form.get("order", "")).split(",") if part.strip()]
        services.reorder(session, _media_root(request), mapping, ids)
    except ValueError as exc:
        session.rollback()
        message = str(exc) if isinstance(exc, ServiceError) else "Invalid order"
        return _list(request, session, mapping, error=message, status_code=422)
    return _list(request, session, mapping)


@router.post("/mappings/{mapping_id}/videos")
async def add_videos(request: Request, mapping_id: int, session: SessionDep):
    mapping = get_or_404(session, Mapping, mapping_id)
    form = await request.form()
    try:
        added = await request.app.state.syncer.add_url(mapping_id, str(form.get("url", "")))
    except (ServiceError, DownloadFailed) as exc:
        return _list(request, session, mapping, error=str(exc), status_code=422)
    session.expire_all()
    mapping = get_or_404(session, Mapping, mapping_id)
    notice = f"Added {added} video(s)" if added else "Nothing new to add (already in this mapping)"
    return _list(request, session, mapping, notice=notice)


@router.post("/mappings/{mapping_id}/ignored/{youtube_id}/unignore")
async def unignore(request: Request, mapping_id: int, youtube_id: str, session: SessionDep):
    services.unignore(session, get_or_404(session, Mapping, mapping_id), youtube_id)
    return RedirectResponse(f"/mappings/{mapping_id}", status_code=303)


@router.get("/videos/{video_id}/status")
async def status(request: Request, video_id: int, session: SessionDep):
    return render(request, "partials/video_status.html", {"v": get_or_404(session, Video, video_id)})


@router.get("/videos/{video_id}/row")
async def row(request: Request, video_id: int, session: SessionDep):
    video = get_or_404(session, Video, video_id)
    return render(request, "partials/video_row.html", list_context(session, video.mapping) | {"v": video})


@router.get("/videos/{video_id}/edit")
async def edit_row(request: Request, video_id: int, session: SessionDep):
    video = get_or_404(session, Video, video_id)
    return render(request, "partials/video_edit_row.html",
                  {"v": video, "mapping": video.mapping, "preview": preview_name(session, video)})


@router.get("/videos/{video_id}/preview")
async def preview(video_id: int, session: SessionDep, title: str | None = None, artist: str | None = None,
                  song: str | None = None):
    video = get_or_404(session, Video, video_id)
    return PlainTextResponse(preview_name(session, video, title=title, artist=artist, song=song))


@router.post("/videos/{video_id}/rename")
async def rename(request: Request, video_id: int, session: SessionDep):
    video = get_or_404(session, Video, video_id)
    form = await request.form()
    try:
        services.rename_video(session, _media_root(request), video, title=form.get("title"),
                              artist=form.get("artist"), song=form.get("song"))
    except ServiceError as exc:
        session.rollback()
        response = render(request, "partials/video_edit_row.html",
                          {"v": video, "mapping": video.mapping, "error": str(exc),
                           "preview": preview_name(session, video)}, status_code=422)
        response.headers["HX-Retarget"] = f"#video-{video.id}"
        response.headers["HX-Reswap"] = "outerHTML"
        return response
    return _list(request, session, video.mapping)


@router.post("/videos/{video_id}/season")
async def season(request: Request, video_id: int, session: SessionDep):
    video = get_or_404(session, Video, video_id)
    mapping = video.mapping
    form = await request.form()
    try:
        services.set_season_break(session, _media_root(request), mapping, video.id, form.get("on") == "1")
    except ServiceError as exc:
        session.rollback()
        return _list(request, session, mapping, error=str(exc), status_code=422)
    return _list(request, session, mapping)


@router.post("/videos/{video_id}/delete")
async def delete(request: Request, video_id: int, session: SessionDep):
    video = get_or_404(session, Video, video_id)
    mapping = video.mapping
    form = await request.form()
    request.app.state.worker.cancel(video.id)
    services.delete_video(session, _media_root(request), video, ignore=form.get("ignore") == "on")
    return _list(request, session, mapping)


@router.post("/videos/{video_id}/retry")
async def retry(request: Request, video_id: int, session: SessionDep):
    video = get_or_404(session, Video, video_id)
    services.retry_video(session, _media_root(request), video)
    request.app.state.worker.kick()
    return _list(request, session, video.mapping)
