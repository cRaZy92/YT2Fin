"""Show images in the mapping folder (poster, backdrop, banner), named the way Jellyfin picks them up.

Only images this app placed are ever replaced: channel images follow the channel, an uploaded image
stays until the user resets it, and a file someone else put there is left alone.
"""
from __future__ import annotations

import logging
import os
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from app.models import Mapping

log = logging.getLogger(__name__)

KINDS = {"poster": "Poster", "fanart": "Backdrop", "banner": "Banner"}
EXTS = (".jpg", ".jpeg", ".png", ".webp")
MAX_BYTES = 25 * 1024 * 1024
CHANNEL, CUSTOM, EPISODE = "channel", "custom", "episode"


class ArtworkError(ValueError):
    pass


def sniff_ext(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def download_image(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ArtworkError(f"Image is larger than {MAX_BYTES // 1024**2} MB")
    return data


def _record(mapping: Mapping, kind: str) -> dict[str, Any]:
    return (mapping.artwork or {}).get(kind) or {}


def _set_record(mapping: Mapping, kind: str, record: dict[str, Any] | None) -> None:
    artwork = dict(mapping.artwork or {})  # a new dict, so SQLAlchemy sees the JSON change
    if record is None:
        artwork.pop(kind, None)
    else:
        artwork[kind] = record
    mapping.artwork = artwork


def _files_on_disk(folder: Path, kind: str) -> list[Path]:
    return [folder / f"{kind}{ext}" for ext in EXTS if (folder / f"{kind}{ext}").is_file()]


def current_file(folder: Path, mapping: Mapping, kind: str) -> Path | None:
    record = _record(mapping, kind)
    if record.get("file") and (folder / record["file"]).is_file():
        return folder / record["file"]
    found = _files_on_disk(folder, kind)
    return found[0] if found else None


def source(folder: Path, mapping: Mapping, kind: str) -> str | None:
    """Where the current image came from: channel, custom, episode, other (not ours) or None (no image)."""
    record = _record(mapping, kind)
    if record.get("file") and (folder / record["file"]).is_file():
        return record["source"]
    return "other" if _files_on_disk(folder, kind) else None


def _is_episode_thumb_copy(folder: Path, image: Path) -> bool:
    """Older versions copied the first episode's thumbnail to poster.jpg without recording it."""
    data = image.read_bytes()
    return any(thumb.read_bytes() == data for thumb in folder.rglob("*-thumb.jpg")
               if thumb.stat().st_size == len(data))


def replaceable(folder: Path, mapping: Mapping, kind: str) -> bool:
    """May an automatic (channel or episode) image be written for this kind?"""
    record = _record(mapping, kind)
    if record:
        return record["source"] != CUSTOM
    found = _files_on_disk(folder, kind)
    if not found:
        return True
    return kind == "poster" and found == [folder / "poster.jpg"] and _is_episode_thumb_copy(folder, found[0])


def install(folder: Path, mapping: Mapping, kind: str, data: bytes, origin: str, url: str | None = None,
            ext: str | None = None) -> None:
    """Write the image as <kind>.<ext> and drop any other <kind>.* so Jellyfin sees only this one."""
    ext = ext or sniff_ext(data)
    if ext is None:
        raise ArtworkError("Not a JPEG, PNG or WebP image")
    target = folder / f"{kind}{ext}"
    temp = folder / f".ytjs-art-{uuid.uuid4().hex}"
    temp.write_bytes(data)
    os.replace(temp, target)
    for other in _files_on_disk(folder, kind):
        if other != target:
            other.unlink(missing_ok=True)
    _set_record(mapping, kind, {"source": origin, "file": target.name, "url": url or _record(mapping, kind).get("url")})


def remove(folder: Path, mapping: Mapping, kind: str) -> None:
    record = _record(mapping, kind)
    if record.get("file"):
        (folder / record["file"]).unlink(missing_ok=True)
    _set_record(mapping, kind, None)


def wanted(folder: Path, mapping: Mapping, images: dict[str, str], force: bool = False) -> dict[str, str]:
    """Channel images that should be (re)downloaded. Uploaded images only remember the URL for a later reset."""
    result: dict[str, str] = {}
    for kind, url in images.items():
        if kind not in KINDS:
            continue
        record = _record(mapping, kind)
        if record.get("source") == CUSTOM:
            if record.get("url") != url:
                _set_record(mapping, kind, record | {"url": url})
            continue
        current = record.get("source") == CHANNEL and record.get("url") == url and (folder / record["file"]).is_file()
        if (force or not current) and replaceable(folder, mapping, kind):
            result[kind] = url
    return result


def download_all(urls: dict[str, str], fetch=None) -> dict[str, bytes]:
    """Blocking: fetch each image; one failing image doesn't stop the others."""
    fetch = fetch or download_image
    images: dict[str, bytes] = {}
    for kind, url in urls.items():
        try:
            images[kind] = fetch(url)
        except (OSError, ArtworkError) as exc:
            log.warning("Could not download the channel %s: %s", KINDS[kind].lower(), exc)
    return images


def owned_files(mapping: Mapping) -> list[str]:
    return [record["file"] for record in (mapping.artwork or {}).values() if record.get("file")]
