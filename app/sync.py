from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy.orm import Session

from app import artwork, organizer, ytdlp_client
from app import settings as settings_mod
from app.errors import ErrorKind
from app.models import Mapping, MappingType, Video, VideoStatus
from app.mvparse import parse_artist_song
from app.services import ServiceError, max_len
from app.timeutil import utcnow
from app.ytdlp_client import DownloadFailed, Entry

log = logging.getLogger(__name__)

SHOW_INFO_MAX_AGE = timedelta(days=7)  # channel art and descriptions rarely change


def title_matches(title_filter: str | None, title: str) -> bool:
    """A mapping's title filter: case-insensitive "contains"; empty matches everything."""
    return not title_filter or title_filter.strip().casefold() in title.casefold()


def add_entries(session: Session, mapping: Mapping, entries: list[Entry], source: str) -> list[Video]:
    """Append unseen entries at the end. Sync skips deleted videos; a manual add restores them in their slot."""
    existing = {v.youtube_id: v for v in mapping.videos}
    next_position = max((v.position for v in mapping.videos), default=0) + 1
    # Original numbers never shrink after deletes, so they stay unique per mapping.
    next_original = max((v.original_position or 0 for v in mapping.videos), default=0) + 1
    added: list[Video] = []
    for entry in entries:
        if entry.youtube_id in existing:
            ghost = existing[entry.youtube_id]
            if source == "manual" and ghost.status == VideoStatus.DELETED:
                ghost.status = VideoStatus.PENDING
                added.append(ghost)
            continue
        if source == "sync" and not title_matches(mapping.title_filter, entry.title):
            continue  # one mapping per series: only titles containing the filter text
        video = Video(youtube_id=entry.youtube_id, yt_title=entry.title, channel=entry.channel,
                      position=next_position, original_position=next_original, source=source,
                      status=VideoStatus.PENDING)
        if mapping.type == MappingType.MUSIC_VIDEO:
            video.artist, video.song = parse_artist_song(entry.title, entry.channel)
        mapping.videos.append(video)
        existing[entry.youtube_id] = video
        next_position += 1
        next_original += 1
        added.append(video)
    session.commit()
    return added


class Syncer:
    def __init__(self, session_factory, config, worker, notifier, fetch=ytdlp_client.fetch_source_entries,
                 fetch_show=None, fetch_image=None) -> None:
        self.session_factory = session_factory
        self.config = config
        self.worker = worker
        self.notifier = notifier
        self._fetch = fetch
        # looked up at call time so tests can stub the network defaults
        self._fetch_show = fetch_show
        self._fetch_image = fetch_image
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
                reverse = mapping.reverse_order
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
            if reverse:
                entries.reverse()  # newest-first playlist: oldest first, so new uploads append at the end
            with self.session_factory() as session:
                mapping = session.get(Mapping, mapping_id)
                if mapping is None:
                    return None
                added = add_entries(session, mapping, entries, source="sync")
                mapping.last_synced_at = utcnow()
                mapping.last_sync_error = None
                session.commit()
                checked = mapping.show_info_checked_at
            if checked is None or utcnow() - checked > SHOW_INFO_MAX_AGE:
                await self._refresh_show_info(mapping_id, url, opts)
            log.info("Sync of %s found %d new video(s)", name, len(added))
            self.notifier.sync_status(mapping_id, f"{len(added)} new video(s)" if added else "Up to date")
            if added:
                self.notifier.list_changed(mapping_id)
                self.worker.kick()
            else:
                self.notifier.mapping_changed(mapping_id)
            return len(added)

    async def refresh_show_info(self, mapping_id: int, force: bool = True) -> None:
        """Re-read description, channel and channel images now; force downloads unchanged images again."""
        async with self._lock:
            with self.session_factory() as session:
                mapping = session.get(Mapping, mapping_id)
                if mapping is None or not mapping.playlist_url:
                    return
                url = mapping.playlist_url
                opts = settings_mod.resolve(session, mapping)
            await self._refresh_show_info(mapping_id, url, opts, force=force)

    async def _refresh_show_info(self, mapping_id: int, url: str, opts: dict, force: bool = False) -> None:
        fetch_show = self._fetch_show or ytdlp_client.fetch_show_info
        try:
            info = await asyncio.to_thread(fetch_show, url, opts, self.config.cookiefile())
        except DownloadFailed as exc:
            log.warning("Could not read the show info: %s", exc.message)  # retried on the next sync
            return
        with self.session_factory() as session:
            mapping = session.get(Mapping, mapping_id)
            if mapping is None:
                return
            mapping.channel_name = info.channel
            mapping.source_description = info.description
            mapping.show_info_checked_at = utcnow()
            urls: dict[str, str] = {}
            folder = None
            if mapping.type == MappingType.SHOW:
                folder = organizer.mapping_dir(self.config.media_root, mapping)
                if folder.is_dir():
                    urls = artwork.wanted(folder, mapping, info.images, force=force)
            session.commit()
        images = await asyncio.to_thread(artwork.download_all, urls, self._fetch_image) if urls else {}
        with self.session_factory() as session:
            mapping = session.get(Mapping, mapping_id)
            if mapping is None:
                return
            for kind, data in images.items():
                try:
                    artwork.install(folder, mapping, kind, data, artwork.CHANNEL, urls[kind])
                except (OSError, artwork.ArtworkError) as exc:
                    log.warning("Could not save the channel %s: %s", artwork.KINDS[kind].lower(), exc)
            session.commit()
            try:
                organizer.apply(session, self.config.media_root, mapping, max_len(session))
            except OSError:
                log.exception("Failed to organize mapping %s", mapping.name)
        self.notifier.mapping_changed(mapping_id)

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
            if added:  # a restored video takes its old number back, so later files shift
                organizer.apply(session, self.config.media_root, mapping, max_len(session))
        if added:
            self.notifier.list_changed(mapping_id)
            self.worker.kick()
        return len(added)
