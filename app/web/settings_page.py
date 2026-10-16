from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import RedirectResponse

from app import settings as settings_mod
from app import updater as updater_mod
from app.settings import SettingsError
from app.web.deps import SessionDep, render

router = APIRouter()


def looks_like_cookies(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    if lines[0].startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        return True
    data = [line for line in lines if not line.startswith("#")]
    return bool(data) and all(len(line.split("\t")) == 7 for line in data)


def _installed_version() -> str:
    try:
        return updater_mod.installed_version()
    except Exception:  # noqa: BLE001
        return "unknown"


def _page(request: Request, session, *, values=None, error=None, status_code=200):
    config = request.app.state.config
    cookie_file = config.cookiefile()
    updater = request.app.state.updater
    return render(request, "settings.html", {
        "v": values or settings_mod.get_all(session),
        "choices": settings_mod.CHOICES,
        "error": error,
        "notice": request.query_params.get("msg"),
        "ytdlp_running": updater_mod.running_version(),
        "ytdlp_installed": _installed_version(),
        "last_update": updater.last_result,
        "last_checked": updater.last_checked_at,
        "cookies_present": cookie_file is not None,
        "cookies_mtime": datetime.fromtimestamp(cookie_file.stat().st_mtime, UTC).replace(tzinfo=None) if cookie_file else None,
    }, status_code=status_code)


def _redirect(message: str) -> RedirectResponse:
    return RedirectResponse("/settings?" + urlencode({"msg": message}), status_code=303)


@router.get("/settings")
async def settings_page(request: Request, session: SessionDep):
    return _page(request, session)


@router.post("/settings")
async def save_settings(request: Request, session: SessionDep):
    form = await request.form()
    raw = {key: form[key] for key in settings_mod.DEFAULTS if key in form and key not in settings_mod.BOOL_KEYS}
    for key in settings_mod.BOOL_KEYS:
        raw[key] = key in form
    try:
        settings_mod.set_many(session, raw)
    except SettingsError as exc:
        session.rollback()
        return _page(request, session, values={**settings_mod.get_all(session), **raw}, error=str(exc), status_code=400)
    request.app.state.worker.kick()
    return _redirect("Settings saved")


@router.post("/settings/cookies")
async def upload_cookies(request: Request, session: SessionDep, file: Annotated[UploadFile, File()]):
    text = (await file.read()).decode("utf-8", errors="replace")
    if not looks_like_cookies(text):
        return _page(request, session, error="That does not look like a Netscape-format cookies.txt file", status_code=400)
    path = request.app.state.config.cookies_path
    # Created with 0600 from the first byte; the cookies are a logged-in YouTube session.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), 0o600)  # an existing file keeps its old mode otherwise
        fh.write(text)
    return _redirect("Cookies saved")


@router.post("/settings/cookies/delete")
async def delete_cookies(request: Request):
    request.app.state.config.cookies_path.unlink(missing_ok=True)
    return _redirect("Cookies removed")


@router.post("/settings/ytdlp/update")
async def update_ytdlp(request: Request):
    return _redirect(await request.app.state.updater.check())
