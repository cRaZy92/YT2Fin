"""Makes the filesystem match the database. The DB is the source of truth."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
import os
import re
import shutil
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import descriptions, naming, nfo
from app.models import Mapping, MappingType, Video, VideoStatus
from app.timeutil import utcnow

log = logging.getLogger(__name__)

THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")
STAGING_NAME = ".ytjs-tmp"
_COMPANION_TAIL = re.compile(r"\.nfo|-thumb\.(?:jpg|jpeg|png|webp)|\.[A-Za-z0-9_-]{1,15}\.(?:srt|vtt|ass)")
_SEASON_DIR = re.compile(r"Season \d+")


class PathError(ValueError):
    pass


def media_problem(media_root: Path, min_free_gb: float = 0) -> str | None:
    """Why the media folder can't be used right now, or None when it's fine."""
    if not media_root.is_dir():
        return f"Media folder {media_root} does not exist (is the volume mounted?)"
    if not os.access(media_root, os.W_OK | os.X_OK):
        return (f"Media folder {media_root} is not writable by user {os.getuid()}:{os.getgid()}. "
                "Fix its owner/permissions or set PUID/PGID to its owner. Downloads are paused until then.")
    if min_free_gb:
        free_gb = shutil.disk_usage(media_root).free / 1024**3
        if free_gb < min_free_gb:
            return (f"Low disk space: {free_gb:.1f} GB free on {media_root} (minimum {min_free_gb:g} GB). "
                    "Downloads are paused until space is freed or the limit is lowered in Settings.")
    return None


def ensure_under_root(media_root: Path, path: Path | str) -> Path:
    root = media_root.resolve()
    resolved = Path(path).resolve()
    if resolved == root or root not in resolved.parents:
        raise PathError(f"{path} must be a folder inside {media_root}")
    staging = root / STAGING_NAME
    if resolved == staging or staging in resolved.parents:
        raise PathError(f"{path} is inside the internal staging folder")
    return resolved


def mapping_dir(media_root: Path, mapping: Mapping) -> Path:
    return ensure_under_root(media_root, mapping.folder)


def ordered_videos(mapping: Mapping) -> list[Video]:
    return sorted(mapping.videos, key=lambda v: v.position)


def naming_items(mapping: Mapping) -> list[naming.NamingItem]:
    break_ids = {b.video_id for b in mapping.season_breaks}
    items: list[naming.NamingItem] = []
    carry = False
    for video in ordered_videos(mapping):
        is_break = video.id in break_ids or carry
        if video.status == VideoStatus.UNAVAILABLE:
            carry = is_break
            continue
        carry = False
        items.append(naming.NamingItem(
            video_id=video.id, title=video.display_title,
            artist=video.artist or "", song=video.song or "", season_break=is_break,
        ))
    return items


def target_stems(mapping: Mapping, max_len: int) -> dict[int, str]:
    return naming.compute_stems(mapping.type, mapping.show_title or mapping.name, naming_items(mapping), max_len)


def companion_files(media_path: Path) -> list[Path]:
    stem = media_path.stem
    if not media_path.parent.is_dir():
        return []
    return [
        p for p in media_path.parent.iterdir()
        if p != media_path and p.is_file() and p.name.startswith(stem)
        and _COMPANION_TAIL.fullmatch(p.name[len(stem):])
    ]


def execute_moves(moves: list[tuple[Path, Path]]) -> None:
    """Rename src->dst in two phases (via unique temp names) so swaps and cycles never collide."""
    staged: list[tuple[Path, Path, Path]] = []  # (original, temp, destination)
    done: list[tuple[Path, Path]] = []  # (original, current location)
    try:
        for src, dst in moves:
            tmp = src.with_name(f".ytjs-move-{uuid.uuid4().hex}")
            os.rename(src, tmp)
            staged.append((src, tmp, dst))
            done.append((src, tmp))
        for index, (src, tmp, dst) in enumerate(staged):
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                raise FileExistsError(f"Refusing to overwrite {dst}")
            os.rename(tmp, dst)
            done[index] = (src, dst)
    except OSError:
        # Put every file back where it was so nothing is left behind under a hidden temp name.
        for src, current in reversed(done):
            try:
                os.rename(current, src)
            except OSError:
                log.exception("Could not restore %s to %s", current, src)
        raise


def _group_moves(folder: Path, current: Path, target_rel: str) -> list[tuple[Path, Path]]:
    target = folder / target_rel
    new_stem = target.stem
    moves = [(current, target)]
    for companion in companion_files(current):
        tail = companion.name[len(current.stem):]
        moves.append((companion, target.with_name(new_stem + tail)))
    return moves


def apply(session: Session, media_root: Path, mapping: Mapping, max_len: int) -> None:
    folder = mapping_dir(media_root, mapping)
    if not folder.is_dir():
        # Never recreate it here: a missing folder usually means an unmounted media volume.
        log.warning("Mapping folder %s is missing; not organizing (is the media volume mounted?)", folder)
        return
    stems = target_stems(mapping, max_len)

    plans: list[tuple[Video, str, list[tuple[Path, Path]]]] = []
    for video in ordered_videos(mapping):
        if video.status != VideoStatus.DONE or not video.file_path or video.id not in stems:
            continue
        current = folder / video.file_path
        target_rel = stems[video.id] + current.suffix
        if video.file_path == target_rel:
            continue
        if not current.exists():
            log.warning("File for video %s missing: %s", video.youtube_id, current)
            continue
        plans.append((video, target_rel, _group_moves(folder, current, target_rel)))

    # Drop plans whose targets are occupied by files that are not themselves moving away.
    # Repeat until stable, because dropping one plan keeps its source file in place.
    accepted = plans
    while True:
        moving = {src for _, _, group in accepted for src, _ in group}
        kept = [p for p in accepted if not any(dst.exists() and dst not in moving for _, dst in p[2])]
        if len(kept) == len(accepted):
            break
        accepted = kept
    for video, target_rel, _ in plans:
        if all(video is not v for v, _, _ in accepted):
            log.error("Not renaming %s to %s: target already exists", video.file_path, target_rel)

    if accepted:
        execute_moves([move for _, _, group in accepted for move in group])
        for video, target_rel, _ in accepted:
            video.file_path = target_rel
        session.commit()

    _backfill_sizes(session, folder, mapping)
    _write_metadata(folder, mapping)
    _remove_empty_dirs(folder)


def _backfill_sizes(session: Session, folder: Path, mapping: Mapping) -> None:
    """Files downloaded before sizes were recorded get size and download time (file mtime) filled in once."""
    changed = False
    for video in mapping.videos:
        if video.status == VideoStatus.DONE and video.file_path and (video.file_size is None or video.downloaded_at is None):
            media = folder / video.file_path
            if media.is_file():
                stat = media.stat()
                if video.file_size is None:
                    video.file_size = stat.st_size
                if video.downloaded_at is None:
                    video.downloaded_at = datetime.fromtimestamp(stat.st_mtime, UTC).replace(tzinfo=None)
                changed = True
    if changed:
        session.commit()


def _write_if_changed(path: Path, content: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.write_text(content, encoding="utf-8")


def _write_metadata(folder: Path, mapping: Mapping) -> None:
    show_title = mapping.show_title or mapping.name
    is_show = mapping.type == MappingType.SHOW
    numbers = naming.season_episode(naming_items(mapping)) if is_show else {}
    remove_terms = descriptions.parse_terms(mapping.description_remove)
    if is_show:
        tvshow = folder / "tvshow.nfo"
        # Never clobber a tvshow.nfo someone else wrote (a curated one, or Jellyfin's own).
        if not tvshow.exists() or nfo.is_ours(tvshow.read_text(encoding="utf-8", errors="replace")):
            _write_if_changed(tvshow, nfo.tvshow_nfo(show_title))
    for video in ordered_videos(mapping):
        if video.status != VideoStatus.DONE or not video.file_path:
            continue
        media = folder / video.file_path
        if not media.exists():
            continue
        if is_show:
            season, episode = numbers[video.id]
            content = nfo.episode_nfo(
                title=video.display_title, show_title=show_title, season=season, episode=episode,
                plot=descriptions.clean_description(video.description, mapping.description_mode, remove_terms),
                aired=video.upload_date, youtube_id=video.youtube_id,
            )
        else:
            content = nfo.musicvideo_nfo(
                title=video.song or video.display_title, artist=video.artist or "",
                plot=descriptions.clean_description(video.description, mapping.description_mode, remove_terms),
                premiered=video.upload_date, youtube_id=video.youtube_id,
            )
        _write_if_changed(media.with_suffix(".nfo"), content)
    if is_show:
        _ensure_poster(folder, mapping)


def _ensure_poster(folder: Path, mapping: Mapping) -> None:
    if any((folder / f"poster{ext}").exists() for ext in THUMB_EXTS):
        return
    for video in ordered_videos(mapping):
        if video.status != VideoStatus.DONE or not video.file_path:
            continue
        media = folder / video.file_path
        thumb = media.with_name(media.stem + "-thumb.jpg")
        if thumb.exists():
            shutil.copyfile(thumb, folder / "poster.jpg")
            return


def _remove_empty_dirs(folder: Path) -> None:
    """Remove empty season folders only; other folders may belong to the user."""
    for path in folder.iterdir():
        if path.is_dir() and _SEASON_DIR.fullmatch(path.name) and not any(path.iterdir()):
            path.rmdir()


def place(session: Session, media_root: Path, mapping: Mapping, video: Video,
          staged_media: Path, staged_extras: list[Path], max_len: int) -> None:
    """Move a finished download into the library.

    The files land under a unique hidden temp name first; apply() then renames them to the
    target name with its collision checks, so a foreign file or a sibling that still holds
    the name is never overwritten.
    """
    folder = mapping_dir(media_root, mapping)
    if not folder.is_dir():
        raise FileNotFoundError(f"Mapping folder {folder} is missing (is the media volume mounted?)")
    temp = folder / f".ytjs-new-{uuid.uuid4().hex}{staged_media.suffix}"
    shutil.move(staged_media, temp)
    video.file_size = temp.stat().st_size
    video.downloaded_at = utcnow()
    for extra in staged_extras:
        tail = extra.name[len(staged_media.stem):]
        if extra.suffix.lower() in THUMB_EXTS:
            tail = "-thumb" + extra.suffix.lower()
        shutil.move(extra, temp.with_name(temp.stem + tail))
    if video.file_path:
        old = folder / video.file_path
        for path in [*companion_files(old), old]:
            path.unlink(missing_ok=True)
    video.status = VideoStatus.DONE
    video.file_path = temp.relative_to(folder).as_posix()
    session.commit()
    apply(session, media_root, mapping, max_len)


def delete_video_files(media_root: Path, mapping: Mapping, video: Video) -> None:
    if not video.file_path:
        return
    media = mapping_dir(media_root, mapping) / video.file_path
    for path in [*companion_files(media), media]:
        path.unlink(missing_ok=True)


def delete_mapping_files(media_root: Path, mapping: Mapping) -> None:
    """Remove only what this app put there; the user's own files in the folder survive."""
    folder = mapping_dir(media_root, mapping)
    if not folder.is_dir():
        return
    for video in mapping.videos:
        delete_video_files(media_root, mapping, video)
    for name in ("tvshow.nfo", "poster.jpg"):
        (folder / name).unlink(missing_ok=True)
    _remove_empty_dirs(folder)
    if not any(folder.iterdir()):
        folder.rmdir()


def reconcile(session: Session, media_root: Path, staging_dir: Path, max_len: int) -> None:
    """Startup repair: clean staging, requeue interrupted/missing downloads, re-apply naming."""
    for video in session.scalars(select(Video).where(Video.status == VideoStatus.DOWNLOADING)).all():
        video.status = VideoStatus.PENDING
    session.commit()
    problem = media_problem(media_root)
    if problem:
        log.error("%s Skipping file checks until it is fixed.", problem)
        return
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    for mapping in session.scalars(select(Mapping)).all():
        try:
            folder = mapping_dir(media_root, mapping)
        except PathError:
            log.error("Mapping %s has an invalid folder %s", mapping.name, mapping.folder)
            continue
        if not folder.is_dir():
            # Don't re-queue the whole library onto an empty mount point.
            log.error("Folder %s of mapping %s is missing; leaving it untouched (is the media volume mounted?)",
                      folder, mapping.name)
            continue
        for video in mapping.videos:
            if video.status == VideoStatus.DONE and (not video.file_path or not (folder / video.file_path).exists()):
                log.warning("File for %s is missing; re-queuing download", video.youtube_id)
                video.status = VideoStatus.PENDING
                video.file_path = None
        session.commit()
        try:
            apply(session, media_root, mapping, max_len)
        except OSError:
            log.exception("Failed to organize mapping %s", mapping.name)
