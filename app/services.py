"""Mapping/video mutations. Each mutation updates the DB, then lets the organizer fix the disk."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import descriptions, organizer
from app import settings as settings_mod
from app.models import Mapping, MappingType, SeasonBreak, Video, VideoStatus


class ServiceError(ValueError):
    pass


@dataclass
class MappingInput:
    name: str
    type: str
    folder: str
    playlist_url: str | None = None
    show_title: str | None = None
    enabled: bool = True
    sync_interval_minutes: int | None = None
    include_shorts: bool = False
    reverse_order: bool = False
    title_filter: str | None = None
    description_mode: str = "cleaned"
    description_remove: str | None = None
    overrides: dict[str, Any] = field(default_factory=dict)


EMPTY_STATS: dict[str, Any] = {s.value: 0 for s in VideoStatus} | {
    "total": 0, "size": 0, "runtime": 0, "seasons": 0, "last_download": None,
}


def max_len(session: Session) -> int:
    return settings_mod.get_all(session)["max_filename_length"]


def _apply(session: Session, media_root: Path, mapping: Mapping) -> None:
    organizer.apply(session, media_root, mapping, max_len(session))


def resolve_folder(media_root: Path, folder: str) -> Path:
    raw = folder.strip()
    if not raw:
        raise ServiceError("Folder is required")
    path = Path(raw)
    if not path.is_absolute():
        path = media_root / path
    try:
        return organizer.ensure_under_root(media_root, path)
    except organizer.PathError as exc:
        raise ServiceError(str(exc)) from exc


def _folder_error(folder: Path, exc: OSError) -> ServiceError:
    if isinstance(exc, PermissionError):
        return ServiceError(
            f"Cannot create {folder}: the media folder is not writable by user {os.getuid()}:{os.getgid()}. "
            "Make the media folder owned by (or writable for) that user, or change PUID/PGID."
        )
    return ServiceError(f"Cannot create {folder}: {exc.strerror or exc}")


def _make_dir(folder: Path) -> None:
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _folder_error(folder, exc) from exc


def _nested(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def _check_overlap(session: Session, folder: Path, exclude_id: int | None = None) -> None:
    for other in session.scalars(select(Mapping)):
        if other.id != exclude_id and _nested(Path(other.folder).resolve(), folder):
            raise ServiceError(f"Folder overlaps with mapping '{other.name}'")


def _validate(data: MappingInput) -> dict[str, Any]:
    if not data.name.strip():
        raise ServiceError("Name is required")
    if data.type not in (MappingType.SHOW, MappingType.MUSIC_VIDEO):
        raise ServiceError("Type must be show or music_video")
    if data.playlist_url and not data.playlist_url.startswith(("http://", "https://")):
        raise ServiceError("Playlist URL must start with http:// or https://")
    if data.description_mode not in descriptions.MODES:
        raise ServiceError(f"Description must be one of: {', '.join(descriptions.MODES)}")
    if data.sync_interval_minutes is not None and data.sync_interval_minutes < 5:
        raise ServiceError("Sync interval must be at least 5 minutes")
    return settings_mod.coerce_overrides(data.overrides or {})


def _assign(mapping: Mapping, data: MappingInput, overrides: dict[str, Any]) -> None:
    mapping.name = data.name.strip()
    playlist_url = (data.playlist_url or "").strip() or None
    if playlist_url != mapping.playlist_url:
        # another source: read its description and channel again on the next sync
        mapping.channel_name = mapping.source_description = mapping.show_info_checked_at = None
    mapping.playlist_url = playlist_url
    mapping.show_title = ((data.show_title or "").strip() or mapping.name) if data.type == MappingType.SHOW else None
    mapping.enabled = data.enabled
    mapping.sync_interval_minutes = data.sync_interval_minutes
    mapping.include_shorts = data.include_shorts
    mapping.reverse_order = data.reverse_order
    mapping.title_filter = (data.title_filter or "").strip() or None
    mapping.description_mode = data.description_mode
    mapping.description_remove = (data.description_remove or "").strip() or None
    mapping.overrides = overrides


def create_mapping(session: Session, media_root: Path, data: MappingInput) -> Mapping:
    overrides = _validate(data)
    folder = resolve_folder(media_root, data.folder)
    _check_overlap(session, folder)
    _make_dir(folder)
    mapping = Mapping(type=data.type, folder=str(folder))
    _assign(mapping, data, overrides)
    session.add(mapping)
    session.commit()
    _apply(session, media_root, mapping)
    return mapping


def update_mapping(session: Session, media_root: Path, mapping: Mapping, data: MappingInput) -> Mapping:
    overrides = _validate(data)
    if data.type != mapping.type:
        raise ServiceError("Mapping type cannot be changed")
    folder = resolve_folder(media_root, data.folder)
    _check_overlap(session, folder, exclude_id=mapping.id)
    old = Path(mapping.folder).resolve()
    if folder != old:
        if _nested(folder, old):
            raise ServiceError("The new folder cannot be inside the old one (or the other way round)")
        if folder.exists() and any(folder.iterdir()):
            raise ServiceError("The new folder already exists and is not empty")
        try:
            folder.parent.mkdir(parents=True, exist_ok=True)
            if folder.exists():
                folder.rmdir()
            if old.exists():
                shutil.move(str(old), str(folder))
            else:
                folder.mkdir(parents=True)
        except OSError as exc:
            raise _folder_error(folder, exc) from exc
        mapping.folder = str(folder)
    _assign(mapping, data, overrides)
    session.commit()
    _apply(session, media_root, mapping)
    return mapping


def delete_mapping(session: Session, media_root: Path, mapping: Mapping, delete_files: bool) -> None:
    if delete_files:
        organizer.delete_mapping_files(media_root, mapping)
    session.delete(mapping)
    session.commit()


def _get_video(mapping: Mapping, video_id: int) -> Video:
    for video in mapping.videos:
        if video.id == video_id:
            return video
    raise ServiceError("Video does not belong to this mapping")


def reorder(session: Session, media_root: Path, mapping: Mapping, ordered_ids: list[int]) -> None:
    current = {v.id: v for v in mapping.videos}
    if sorted(ordered_ids) != sorted(current):
        raise ServiceError("The new order must contain every video exactly once")
    for position, video_id in enumerate(ordered_ids, start=1):
        current[video_id].position = position
    session.commit()
    _apply(session, media_root, mapping)


def set_season_break(session: Session, media_root: Path, mapping: Mapping, video_id: int, on: bool) -> None:
    video = _get_video(mapping, video_id)
    if video.status == VideoStatus.DELETED:
        raise ServiceError("A deleted video cannot start a season; restore it first")
    existing = next((b for b in mapping.season_breaks if b.video_id == video_id), None)
    if on and existing is None:
        if video is organizer.ordered_videos(mapping)[0]:
            raise ServiceError("Season 1 always starts at the first video")
        mapping.season_breaks.append(SeasonBreak(video_id=video_id))
    elif not on and existing is not None:
        mapping.season_breaks.remove(existing)
    session.commit()
    _apply(session, media_root, mapping)


def rename_video(session: Session, media_root: Path, video: Video, *, title: str | None = None,
                 artist: str | None = None, song: str | None = None) -> None:
    mapping = video.mapping
    if mapping.type == MappingType.SHOW:
        title = (title or "").strip()
        # Same as the YouTube title (e.g. saved unchanged from the prefilled box) = not a custom title
        video.custom_title = title if title and title != video.yt_title else None
    else:
        artist, song = (artist or "").strip(), (song or "").strip()
        if not artist or not song:
            raise ServiceError("Artist and song are both required")
        video.artist, video.song = artist, song
    video.user_edited = True
    session.commit()
    _apply(session, media_root, mapping)


def delete_video(session: Session, media_root: Path, video: Video) -> None:
    """Delete the files but keep the row as a ghost in its slot: unnumbered, skipped by sync, restorable."""
    mapping = video.mapping
    ordered = organizer.ordered_videos(mapping)
    brk = next((b for b in mapping.season_breaks if b.video_id == video.id), None)
    if brk is not None:
        mapping.season_breaks.remove(brk)
        index = ordered.index(video)
        following = ordered[index + 1] if index + 1 < len(ordered) else None
        if following is not None and all(b.video_id != following.id for b in mapping.season_breaks):
            mapping.season_breaks.append(SeasonBreak(video_id=following.id))
        session.flush()
    organizer.delete_video_files(media_root, mapping, video)
    video.status = VideoStatus.DELETED
    video.file_path = video.file_size = video.downloaded_at = None
    video.error = video.next_retry_at = None
    video.attempts = 0
    session.commit()
    _apply(session, media_root, mapping)


def restore_video(session: Session, media_root: Path, video: Video) -> None:
    """Bring a ghost back in its slot; it downloads again and the videos after it shift back."""
    if video.status != VideoStatus.DELETED:
        raise ServiceError("This video is not deleted")
    video.status = VideoStatus.PENDING
    session.commit()
    _apply(session, media_root, video.mapping)


def retry_video(session: Session, media_root: Path, video: Video) -> None:
    video.status = VideoStatus.PENDING
    video.attempts = 0
    video.error = None
    video.next_retry_at = None
    session.commit()
    _apply(session, media_root, video.mapping)  # an unavailable video counts again, so numbers shift


def mapping_stats(session: Session) -> dict[int, dict[str, Any]]:
    """Per mapping: counts by status, plus size, runtime and last download of downloaded videos."""
    stats: dict[int, dict[str, Any]] = {}
    rows = session.execute(
        select(Video.mapping_id, Video.status, func.count()).group_by(Video.mapping_id, Video.status)
    )
    for mapping_id, status, count in rows:
        entry = stats.setdefault(mapping_id, dict(EMPTY_STATS))
        entry[status] = count
        if status != VideoStatus.DELETED:
            entry["total"] += count
    done = session.execute(
        select(Video.mapping_id, func.sum(Video.file_size), func.sum(Video.duration), func.max(Video.downloaded_at))
        .where(Video.status == VideoStatus.DONE).group_by(Video.mapping_id)
    )
    for mapping_id, size, runtime, last in done:
        entry = stats.setdefault(mapping_id, dict(EMPTY_STATS))
        entry.update(size=size or 0, runtime=runtime or 0, last_download=last)
    breaks = dict(session.execute(select(SeasonBreak.mapping_id, func.count()).group_by(SeasonBreak.mapping_id)).all())
    for mapping_id, mapping_type in session.execute(select(Mapping.id, Mapping.type)):
        if mapping_type == MappingType.SHOW and mapping_id in stats:
            stats[mapping_id]["seasons"] = 1 + breaks.get(mapping_id, 0)
    return stats
