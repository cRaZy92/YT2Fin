"""Dry run of a mapping: what a sync would add, how files would be named, how descriptions come out."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import descriptions, naming
from app.models import MappingType
from app.mvparse import parse_artist_song
from app.services import MappingInput, ServiceError
from app.sync import title_matches
from app.ytdlp_client import DownloadFailed, fetch_source_entries, fetch_video_info

ROWS_SHOWN = 10
DESCRIPTION_SAMPLES = 3


@dataclass
class PreviewRow:
    youtube_id: str
    title: str
    included: bool
    file_name: str | None


@dataclass
class DescriptionSample:
    youtube_id: str
    title: str
    original: str | None
    result: str | None


@dataclass
class Preview:
    total: int
    matched: int
    rows: list[PreviewRow] = field(default_factory=list)
    samples: list[DescriptionSample] = field(default_factory=list)
    is_show: bool = True


async def build_preview(data: MappingInput, settings: dict[str, Any], cookiefile: Path | None) -> Preview:
    url = (data.playlist_url or "").strip()
    if not url:
        raise ServiceError("Paste a playlist or channel URL to preview it")
    try:
        entries = await asyncio.to_thread(fetch_source_entries, url, settings, cookiefile, data.include_shorts)
    except DownloadFailed as exc:
        raise ServiceError(f"YouTube said: {exc.message}") from exc

    matched = [e for e in entries if title_matches(data.title_filter, e.title)]
    is_show = data.type == MappingType.SHOW
    items = []
    for index, entry in enumerate(matched):
        artist, song = ("", "") if is_show else parse_artist_song(entry.title, entry.channel)
        items.append(naming.NamingItem(video_id=index, title=entry.title, artist=artist, song=song))
    show_title = (data.show_title or data.name or "Show").strip()
    stems = naming.compute_stems(data.type, show_title, items, settings["max_filename_length"])
    names = {matched[i].youtube_id: f"{stem}.{settings['container']}" for i, stem in stems.items()}

    result = Preview(total=len(entries), matched=len(matched), is_show=is_show)
    for entry in entries[:ROWS_SHOWN]:
        result.rows.append(PreviewRow(entry.youtube_id, entry.title, entry.youtube_id in names,
                                      names.get(entry.youtube_id)))

    remove_terms = descriptions.parse_terms(data.description_remove)
    for entry in matched[:DESCRIPTION_SAMPLES]:
        try:
            info = await asyncio.to_thread(fetch_video_info, entry.youtube_id, settings, cookiefile)
        except DownloadFailed:
            continue  # private/members-only etc.: just leave it out of the sample
        result.samples.append(DescriptionSample(
            entry.youtube_id, entry.title, info.description,
            descriptions.clean_description(info.description, data.description_mode, remove_terms),
        ))
    return result
