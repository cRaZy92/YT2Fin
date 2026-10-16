"""Makes the filesystem match the database. The DB is the source of truth."""
from __future__ import annotations

import logging
import os
import re
import shutil
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app import naming, nfo
from app.models import Mapping, MappingType, Video, VideoStatus

log = logging.getLogger(__name__)

THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")
STAGING_NAME = ".ytjs-tmp"
_COMPANION_TAIL = re.compile(r"\.nfo|-thumb\.(?:jpg|jpeg|png|webp)|\.[A-Za-z0-9_-]{1,15}\.(?:srt|vtt|ass)")
_SEASON_DIR = re.compile(r"Season \d+")


class PathError(ValueError):
    pass


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
    staged: list[tuple[Path, Path]] = []
    for src, dst in moves:
        tmp = src.with_name(f".ytjs-move-{uuid.uuid4().hex}")
        os.rename(src, tmp)
        staged.append((tmp, dst))
    for tmp, dst in staged:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            raise FileExistsError(f"Refusing to overwrite {dst}")
        os.rename(tmp, dst)


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
    folder.mkdir(parents=True, exist_ok=True)
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

    _write_metadata(folder, mapping)
    _remove_empty_dirs(folder)


def _write_if_changed(path: Path, content: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.write_text(content, encoding="utf-8")


def _write_metadata(folder: Path, mapping: Mapping) -> None:
    show_title = mapping.show_title or mapping.name
    is_show = mapping.type == MappingType.SHOW
    numbers = naming.season_episode(naming_items(mapping)) if is_show else {}
    if is_show:
        _write_if_changed(folder / "tvshow.nfo", nfo.tvshow_nfo(show_title))
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
                plot=video.description, aired=video.upload_date, youtube_id=video.youtube_id,
            )
        else:
            content = nfo.musicvideo_nfo(
                title=video.song or video.display_title, artist=video.artist or "",
                plot=video.description, premiered=video.upload_date, youtube_id=video.youtube_id,
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
    for path in sorted((p for p in folder.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(path.iterdir()):
            path.rmdir()
