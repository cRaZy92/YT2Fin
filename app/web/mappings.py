from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from app import preview, services
from app import settings as settings_mod
from app.models import Mapping
from app.naming import sanitize
from app.services import MappingInput, ServiceError
from app.web.deps import SessionDep, done, get_or_404, render, spawn

router = APIRouter()

OVERRIDE_FIELDS = [
    ("max_height", "Max resolution", "choice"),
    ("container", "Container", "choice"),
    ("vcodec", "Preferred codec", "choice"),
    ("subtitles", "Subtitles", "choice"),
    ("subtitle_langs", "Subtitle languages", "text"),
    ("subtitle_auto", "Auto-generated subtitles", "bool"),
    ("sponsorblock", "SponsorBlock", "choice"),
    ("sponsorblock_categories", "SponsorBlock categories", "text"),
]

NEW_VALUES: dict[str, Any] = {
    "name": "", "type": "show", "folder": "", "playlist_url": "", "show_title": "",
    "enabled": True, "sync_interval_minutes": "", "include_shorts": False, "title_filter": "",
    "description_mode": "cleaned", "description_remove": "", "overrides": {},
}


def _form_to_input(form) -> MappingInput:
    interval = (form.get("sync_interval_minutes") or "").strip()
    try:
        interval_value = int(interval) if interval else None
    except ValueError as exc:
        raise ServiceError("Sync interval must be a whole number of minutes") from exc
    return MappingInput(
        name=form.get("name", ""),
        type=form.get("type", ""),
        folder=form.get("folder", ""),
        playlist_url=(form.get("playlist_url") or "").strip() or None,
        show_title=(form.get("show_title") or "").strip() or None,
        enabled=form.get("enabled") == "on",
        sync_interval_minutes=interval_value,
        include_shorts=form.get("include_shorts") == "on",
        title_filter=form.get("title_filter"),
        description_mode=form.get("description_mode") or "cleaned",
        description_remove=form.get("description_remove"),
        overrides={key: form.get(f"ov_{key}", "") for key in settings_mod.OVERRIDABLE},
    )


def _values_from_form(form) -> dict[str, Any]:
    return {
        "name": form.get("name", ""), "type": form.get("type", "show"), "folder": form.get("folder", ""),
        "playlist_url": form.get("playlist_url", ""), "show_title": form.get("show_title", ""),
        "enabled": form.get("enabled") == "on", "sync_interval_minutes": form.get("sync_interval_minutes", ""),
        "include_shorts": form.get("include_shorts") == "on", "title_filter": form.get("title_filter", ""),
        "description_mode": form.get("description_mode") or "cleaned", "description_remove": form.get("description_remove", ""),
        "overrides": {k: form.get(f"ov_{k}") for k in settings_mod.OVERRIDABLE if form.get(f"ov_{k}")},
    }


def _values_from_mapping(mapping: Mapping, media_root: Path) -> dict[str, Any]:
    folder = Path(mapping.folder)
    try:
        folder_value = folder.relative_to(media_root.resolve()).as_posix()
    except ValueError:
        folder_value = mapping.folder
    return {
        "name": mapping.name, "type": mapping.type, "folder": folder_value,
        "playlist_url": mapping.playlist_url or "", "show_title": mapping.show_title or "",
        "enabled": mapping.enabled, "sync_interval_minutes": mapping.sync_interval_minutes or "",
        "include_shorts": mapping.include_shorts, "title_filter": mapping.title_filter or "",
        "description_mode": mapping.description_mode, "description_remove": mapping.description_remove or "",
        "overrides": {k: v for k, v in (mapping.overrides or {}).items() if v is not None},
    }


def _form_page(request: Request, session, mapping: Mapping | None, values: dict, error: str | None = None,
               status_code: int = 200):
    return render(request, "mapping_form.html", {
        "mapping": mapping, "v": values, "error": error, "choices": settings_mod.CHOICES,
        "override_fields": OVERRIDE_FIELDS, "globals": settings_mod.get_all(session),
        "media_root": str(request.app.state.config.media_root),
    }, status_code=status_code)


@router.get("/mappings/new")
async def new_mapping(request: Request, session: SessionDep):
    return _form_page(request, session, None, dict(NEW_VALUES))


@router.post("/mappings")
async def create_mapping(request: Request, session: SessionDep):
    form = await request.form()
    try:
        mapping = services.create_mapping(session, request.app.state.config.media_root, _form_to_input(form))
    except ValueError as exc:
        session.rollback()
        return _form_page(request, session, None, _values_from_form(form), str(exc), 400)
    if mapping.playlist_url:
        spawn(request.app, request.app.state.syncer.sync(mapping.id))
    return RedirectResponse(f"/mappings/{mapping.id}", status_code=303)


@router.post("/mappings/preview")
async def preview_mapping(request: Request, session: SessionDep):
    """Dry run of the (unsaved) form: nothing is stored or downloaded."""
    form = await request.form()
    if request.app.state.worker.cooldown_until is not None:
        return render(request, "partials/mapping_preview.html",
                      {"error": "YouTube is rate-limiting us right now; try the preview again later."})
    try:
        data = _form_to_input(form)
        values = settings_mod.get_all(session) | settings_mod.coerce_overrides(data.overrides or {})
        result = await preview.build_preview(data, values, request.app.state.config.cookiefile())
    except ValueError as exc:
        return render(request, "partials/mapping_preview.html", {"error": str(exc)})
    return render(request, "partials/mapping_preview.html", {"p": result})


@router.get("/mappings/{mapping_id}/edit")
async def edit_mapping(request: Request, mapping_id: int, session: SessionDep):
    mapping = get_or_404(session, Mapping, mapping_id)
    return _form_page(request, session, mapping, _values_from_mapping(mapping, request.app.state.config.media_root))


@router.post("/mappings/{mapping_id}/edit")
async def update_mapping(request: Request, mapping_id: int, session: SessionDep):
    mapping = get_or_404(session, Mapping, mapping_id)
    form = await request.form()
    try:
        services.update_mapping(session, request.app.state.config.media_root, mapping, _form_to_input(form))
    except ValueError as exc:
        session.rollback()
        return _form_page(request, session, mapping, _values_from_form(form), str(exc), 400)
    return RedirectResponse(f"/mappings/{mapping.id}", status_code=303)


@router.post("/mappings/{mapping_id}/delete")
async def delete_mapping(request: Request, mapping_id: int, session: SessionDep):
    mapping = get_or_404(session, Mapping, mapping_id)
    form = await request.form()
    request.app.state.worker.cancel_mapping(mapping_id)
    services.delete_mapping(session, request.app.state.config.media_root, mapping,
                            delete_files=form.get("delete_files") == "on")
    return RedirectResponse("/", status_code=303)


@router.post("/mappings/{mapping_id}/sync")
async def sync_now(request: Request, mapping_id: int, session: SessionDep):
    get_or_404(session, Mapping, mapping_id)
    if request.app.state.worker.cooldown_until is not None:
        return PlainTextResponse("Syncing is paused while YouTube rate-limits us", status_code=409)
    spawn(request.app, request.app.state.syncer.sync(mapping_id))
    return done(request, f"/mappings/{mapping_id}")


def _browse_dir(media_root: Path, rel: str) -> Path:
    root = media_root.resolve()
    path = (root / rel).resolve()
    if path != root and root not in path.parents:
        raise HTTPException(status_code=400, detail="Folder must be inside the media root")
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="Folder not found")
    return path


def _picker(request: Request, rel: str):
    root = request.app.state.config.media_root.resolve()
    current = _browse_dir(root, rel)
    rel_path = "" if current == root else current.relative_to(root).as_posix()
    subdirs = sorted((p.name for p in current.iterdir() if p.is_dir() and not p.name.startswith(".")), key=str.casefold)
    parent = None
    if current != root:
        parent = "" if current.parent == root else current.parent.relative_to(root).as_posix()
    return render(request, "partials/folder_picker.html",
                  {"rel": rel_path, "subdirs": subdirs, "parent": parent, "root": str(root)})


@router.get("/folders")
async def folders(request: Request, path: str = ""):
    return _picker(request, path)


@router.post("/folders")
async def create_folder(request: Request):
    form = await request.form()
    rel = form.get("path", "")
    base = _browse_dir(request.app.state.config.media_root, rel)
    name = sanitize(form.get("new_folder_name", ""), 120, fallback="")
    if not name:
        raise HTTPException(status_code=400, detail="Enter a folder name")
    (base / name).mkdir(exist_ok=True)
    return _picker(request, rel)
