from __future__ import annotations

import asyncio
import logging

from sqlalchemy.orm import Session

from app import settings as settings_mod
from app import ytdlp_client
from app.errors import ErrorKind
from app.models import Mapping, MappingType, Video, VideoStatus
from app.mvparse import parse_artist_song
from app.services import ServiceError
from app.timeutil import utcnow
from app.ytdlp_client import DownloadFailed, Entry

log = logging.getLogger(__name__)


def add_entries(session: Session, mapping: Mapping, entries: list[Entry], source: str) -> list[Video]:
    """Append unseen entries at the end. Sync skips ignored ids; a manual add un-ignores them."""
    existing = {v.youtube_id for v in mapping.videos}
    ignored = {i.youtube_id: i for i in mapping.ignored}
    next_position = max((v.position for v in mapping.videos), default=0) + 1
    added: list[Video] = []
    title_filter = (mapping.title_filter or "").casefold()
    for entry in entries:
        if entry.youtube_id in existing:
            continue
        if source == "sync" and title_filter and title_filter not in entry.title.casefold():
            continue  # one mapping per series: only titles containing the filter text
        if entry.youtube_id in ignored:
            if source == "sync":
                continue
            mapping.ignored.remove(ignored.pop(entry.youtube_id))
        video = Video(youtube_id=entry.youtube_id, yt_title=entry.title, channel=entry.channel,
                      position=next_position, source=source, status=VideoStatus.PENDING)
        if mapping.type == MappingType.MUSIC_VIDEO:
            video.artist, video.song = parse_artist_song(entry.title, entry.channel)
        mapping.videos.append(video)
        existing.add(entry.youtube_id)
        next_position += 1
        added.append(video)
    session.commit()
    return added


class Syncer:
    def __init__(self, session_factory, config, worker, notifier, fetch=ytdlp_client.fetch_source_entries) -> None:
        self.session_factory = session_factory
        self.config = config
        self.worker = worker
        self.notifier = notifier
        self._fetch = fetch
        self._lock = asyncio.Lock()
        self.running: set[int] = set()

    async def sync(self, mapping_id: int) -> int | None:
        async with self._lock:
            with self.session_factory() as session:
                mapping = session.get(Mapping, mapping_id)
                if mapping is None or not mapping.playlist_url:
                    return None
                url = mapping.playlist_url
                include_shorts = mapping.include_shorts
                opts = settings_mod.resolve(session, mapping)
                name = mapping.name
            self.running.add(mapping_id)
            self.notifier.sync_status(mapping_id, "Checking playlist…")
            log.info("Syncing %s", name)
            try:
                entries = await asyncio.to_thread(self._fetch, url, opts, self.config.cookiefile(), include_shorts)
            except DownloadFailed as exc:
                log.warning("Sync of %s failed: %s", name, exc.message)
                self._record(mapping_id, exc.message)
                if exc.kind == ErrorKind.RATE_LIMIT:
                    self.worker.enter_cooldown(exc.message)
                self.notifier.sync_status(mapping_id, f"Sync failed: {exc.message}")
                return None
            finally:
                self.running.discard(mapping_id)
            with self.session_factory() as session:
                mapping = session.get(Mapping, mapping_id)
                if mapping is None:
                    return None
                added = add_entries(session, mapping, entries, source="sync")
                mapping.last_synced_at = utcnow()
                mapping.last_sync_error = None
                session.commit()
            log.info("Sync of %s found %d new video(s)", name, len(added))
            self.notifier.sync_status(mapping_id, f"{len(added)} new video(s)" if added else "Up to date")
            if added:
                self.notifier.list_changed(mapping_id)
                self.worker.kick()
            else:
                self.notifier.mapping_changed(mapping_id)
            return len(added)

    def _record(self, mapping_id: int, error: str) -> None:
        with self.session_factory() as session:
            mapping = session.get(Mapping, mapping_id)
            if mapping is not None:
                mapping.last_synced_at = utcnow()
                mapping.last_sync_error = error
                session.commit()
        self.notifier.mapping_changed(mapping_id)

    async def add_url(self, mapping_id: int, url: str) -> int:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise ServiceError("Enter a YouTube video or playlist URL")
        with self.session_factory() as session:
            mapping = session.get(Mapping, mapping_id)
            if mapping is None:
                raise ServiceError("Mapping not found")
            opts = settings_mod.resolve(session, mapping)
            include_shorts = mapping.include_shorts
        try:
            entries = await asyncio.to_thread(self._fetch, url, opts, self.config.cookiefile(), include_shorts)
        except DownloadFailed as exc:
            if exc.kind == ErrorKind.RATE_LIMIT:
                self.worker.enter_cooldown(exc.message)
            raise
        with self.session_factory() as session:
            mapping = session.get(Mapping, mapping_id)
            if mapping is None:
                raise ServiceError("Mapping not found")
            added = add_entries(session, mapping, entries, source="manual")
        if added:
            self.notifier.list_changed(mapping_id)
            self.worker.kick()
        return len(added)
