"""Blank the description yt-dlp used to embed in downloaded files, so Jellyfin can't fall back to it."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path

from sqlalchemy import select

from app import organizer
from app.models import Mapping, VideoStatus

log = logging.getLogger(__name__)

TAGS = ("description", "synopsis")


def has_description(path: Path) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    tags = (json.loads(out or "{}").get("format") or {}).get("tags") or {}
    return any(key.lower() in TAGS and str(value).strip() for key, value in tags.items())


def strip(path: Path) -> None:
    """Remux `path` without the description tags (streams copied, mtime kept), replacing it in place."""
    before = path.stat()
    tmp = path.with_name(f".{path.stem}.ytjs-strip{path.suffix}")
    blank = [arg for tag in TAGS for arg in ("-metadata", f"{tag}=")]
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path), "-map", "0", "-dn", "-c", "copy",
             *blank, str(tmp)],
            capture_output=True, text=True, check=True,
        )
        now = path.stat()  # renamed or replaced while we were copying: leave it alone
        if (now.st_ino, now.st_mtime_ns) != (before.st_ino, before.st_mtime_ns):
            raise FileNotFoundError(path)
        os.utime(tmp, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def downloaded_files(session, media_root: Path) -> list[Path]:
    files = []
    for mapping in session.scalars(select(Mapping)).all():
        try:
            folder = organizer.mapping_dir(media_root, mapping)
        except organizer.PathError:
            continue
        files += [folder / v.file_path for v in mapping.videos if v.status == VideoStatus.DONE and v.file_path]
    return [path for path in files if path.exists()]


class Stripper:
    """Background job behind the Settings button; one run at a time."""

    def __init__(self, session_factory, media_root: Path, *, check=has_description, rewrite=strip) -> None:
        self.session_factory = session_factory
        self.media_root = media_root
        self._check = check
        self._rewrite = rewrite
        self.running = False
        self.done = 0
        self.total = 0
        self.last_result: str | None = None

    async def run(self) -> None:
        if self.running:
            return
        self.running, self.done = True, 0
        try:
            with self.session_factory() as session:
                files = await asyncio.to_thread(downloaded_files, session, self.media_root)
            self.total = len(files)
            log.info("Removing embedded descriptions from %d downloaded files", self.total)
            stripped, failed = 0, 0
            for path in files:
                try:
                    if await asyncio.to_thread(self._check, path):
                        await asyncio.to_thread(self._rewrite, path)
                        stripped += 1
                except (OSError, subprocess.CalledProcessError, ValueError) as exc:
                    failed += 1
                    detail = getattr(exc, "stderr", None) or exc
                    log.error("Could not remove the embedded description from %s: %s", path, str(detail).strip())
                self.done += 1
            self.last_result = (f"Removed the embedded description from {stripped} of {self.total} files"
                                + (f"; {failed} failed (see Logs)" if failed else ""))
            log.info(self.last_result)
        finally:
            self.running = False
