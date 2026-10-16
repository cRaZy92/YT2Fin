"""Download queue: one asyncio loop, yt-dlp in threads, all DB/FS work on the loop thread."""
from __future__ import annotations

import asyncio
import itertools
import logging
import random
import shutil
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, func, or_, select

from app import organizer
from app import settings as settings_mod
from app import ytdlp_client
from app.activity import ActiveItem, ActivitySnapshot
from app.errors import ErrorKind
from app.models import Mapping, MappingType, Video, VideoStatus
from app.mvparse import parse_artist_song
from app.timeutil import utcnow
from app.ytdlp_client import Cancelled, DownloadFailed, VideoInfo

log = logging.getLogger(__name__)


def backoff_delay(attempts: int, schedule: list[int]) -> timedelta | None:
    if attempts < 1 or attempts > len(schedule):
        return None
    return timedelta(minutes=schedule[attempts - 1])


def cooldown_minutes(level: int, base: int, cap: int) -> int:
    return min(base * 2 ** max(level - 1, 0), cap)


def _apply_info(video: Video, info: VideoInfo, mapping_type: str) -> None:
    video.yt_title = info.title
    video.channel = info.channel
    video.description = info.description
    video.upload_date = info.upload_date
    video.duration = info.duration
    video.thumbnail_url = info.thumbnail_url
    video.yt_artist = info.artist
    video.yt_track = info.track
    if mapping_type == MappingType.MUSIC_VIDEO and not video.user_edited:
        video.artist, video.song = parse_artist_song(info.title, info.channel, info.artist, info.track)


class Worker:
    POLL_SECONDS = 60.0
    PROGRESS_INTERVAL = 0.5

    def __init__(self, session_factory, config, notifier, *, download=ytdlp_client.download,
                 now: Callable[[], datetime] = utcnow, rng: random.Random | None = None) -> None:
        self.session_factory = session_factory
        self.config = config
        self.notifier = notifier
        self._download = download
        self._now = now
        self._rng = rng or random.Random()
        self.paused = False
        self.cooldown_until: datetime | None = None
        self.cooldown_reason: str | None = None
        self.cooldown_level = 0
        self.next_download_at: datetime | None = None
        self.restart_pending = False
        self.on_idle: Callable[[], None] | None = None
        self.active: dict[int, ActiveItem] = {}
        self._tasks: set[asyncio.Task] = set()
        self._cancelled: set[int] = set()
        self._wake = asyncio.Event()
        self._turns = itertools.count()
        self._last_turn: dict[int, int] = {}
        self._stopping = False

    # ----- control -------------------------------------------------------------------------
    def kick(self) -> None:
        self._wake.set()

    def pause(self) -> None:
        self.paused = True
        self.publish_activity()

    def resume(self) -> None:
        self.paused = False
        self.cooldown_until = None
        self.cooldown_reason = None
        self.kick()
        self.publish_activity()

    def cancel(self, video_id: int) -> None:
        if video_id in self.active:
            self._cancelled.add(video_id)

    def cancel_mapping(self, mapping_id: int) -> None:
        for item in self.active.values():
            if item.mapping_id == mapping_id:
                self._cancelled.add(item.video_id)

    def enter_cooldown(self, reason: str) -> None:
        with self.session_factory() as session:
            values = settings_mod.get_all(session)
        self.cooldown_level += 1
        minutes = cooldown_minutes(self.cooldown_level, values["cooldown_minutes"], values["cooldown_max_minutes"])
        self.cooldown_until = self._now() + timedelta(minutes=minutes)
        self.cooldown_reason = reason[:200]
        log.warning("YouTube rate limit hit (%s); pausing downloads for %d minutes", reason, minutes)
        self.publish_activity()

    def is_blocked(self) -> bool:
        if self.cooldown_until is not None and self._now() >= self.cooldown_until:
            self.cooldown_until = None
            self.cooldown_reason = None
        return self.paused or self.cooldown_until is not None

    # ----- state ---------------------------------------------------------------------------
    def _eligible(self):
        now = self._now()
        return and_(
            Mapping.enabled.is_(True),
            Video.id.not_in(list(self.active)),
            or_(
                Video.status == VideoStatus.PENDING,
                and_(Video.status == VideoStatus.FAILED, Video.next_retry_at.is_not(None), Video.next_retry_at <= now),
            ),
        )

    def snapshot(self) -> ActivitySnapshot:
        with self.session_factory() as session:
            queued = session.scalar(
                select(func.count(Video.id)).select_from(Video).join(Video.mapping).where(self._eligible())
            ) or 0
        return ActivitySnapshot(
            paused=self.paused, cooldown_until=self.cooldown_until, cooldown_reason=self.cooldown_reason,
            next_download_at=self.next_download_at, active=list(self.active.values()), queued=queued,
            restart_pending=self.restart_pending,
            media_problem=organizer.media_problem(self.config.media_root),
        )

    def publish_activity(self) -> None:
        self.notifier.activity(self.snapshot())

    # ----- loop ----------------------------------------------------------------------------
    async def run(self) -> None:
        self.publish_activity()
        while not self._stopping:
            self._wake.clear()
            try:
                delay = self.step()
            except Exception:
                log.exception("Worker step failed")
                delay = self.POLL_SECONDS
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(0)

    async def stop(self) -> None:
        self._stopping = True
        self._cancelled.update(self.active)
        self.kick()
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=15)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def step(self) -> float:
        """Start at most one download. Returns how long to wait before stepping again."""
        now = self._now()
        if self.restart_pending:
            if not self.active and self.on_idle is not None:
                self.on_idle()
            return self.POLL_SECONDS
        if self.is_blocked():
            if self.cooldown_until is not None:
                return max(1.0, min((self.cooldown_until - now).total_seconds(), self.POLL_SECONDS))
            return self.POLL_SECONDS
        if organizer.media_problem(self.config.media_root):
            return self.POLL_SECONDS
        if self.next_download_at is not None and now < self.next_download_at:
            return min((self.next_download_at - now).total_seconds(), self.POLL_SECONDS)
        with self.session_factory() as session:
            if len(self.active) >= settings_mod.get_all(session)["concurrency"]:
                return self.POLL_SECONDS
            video_id = self._next_video_id(session)
            if video_id is None:
                return self.POLL_SECONDS
            self._start(session, video_id)
        return 0.0

    def _next_video_id(self, session) -> int | None:
        """Take turns between mappings so one huge channel backfill can't starve the others."""
        heads = session.execute(
            select(Video.mapping_id, func.min(Video.position)).join(Video.mapping)
            .where(self._eligible()).group_by(Video.mapping_id)
        ).all()
        if not heads:
            return None
        mapping_id, position = min(heads, key=lambda row: (self._last_turn.get(row[0], -1), row[0]))
        self._last_turn[mapping_id] = next(self._turns)
        return session.scalar(
            select(Video.id).join(Video.mapping)
            .where(self._eligible(), Video.mapping_id == mapping_id, Video.position == position)
            .order_by(Video.id).limit(1)
        )

    def _start(self, session, video_id: int) -> None:
        video = session.get(Video, video_id)
        mapping = video.mapping
        video.status = VideoStatus.DOWNLOADING
        video.error = None
        session.commit()
        opts = settings_mod.resolve(session, mapping)
        self.active[video_id] = ActiveItem(video_id=video_id, mapping_id=mapping.id,
                                           title=video.display_title, mapping_name=mapping.name)
        task = asyncio.create_task(self._process(video_id, video.youtube_id, opts))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        log.info("Downloading %s (%s)", video.display_title, video.youtube_id)
        self.notifier.video_changed(video_id, mapping.id)
        self.publish_activity()

    async def _process(self, video_id: int, youtube_id: str, opts: dict) -> None:
        staging = self.config.staging_dir / str(video_id)
        shutil.rmtree(staging, ignore_errors=True)
        loop = asyncio.get_running_loop()
        last_sent = 0.0

        def on_progress(data: dict) -> None:
            nonlocal last_sent
            now = time.monotonic()
            if data.get("stage") == "downloading" and now - last_sent < self.PROGRESS_INTERVAL:
                return
            last_sent = now
            loop.call_soon_threadsafe(self._update_progress, video_id, data)

        try:
            info, media, extras = await asyncio.to_thread(
                self._download, youtube_id, opts, staging, self.config.cookiefile(),
                on_progress, lambda: video_id in self._cancelled,
            )
        except Cancelled:
            log.info("Download of %s cancelled", youtube_id)
        except DownloadFailed as exc:
            self._on_failure(video_id, exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected error downloading %s", youtube_id)
            self._on_failure(video_id, DownloadFailed(ErrorKind.TRANSIENT, str(exc)))
        else:
            try:
                self._on_success(video_id, info, media, extras, opts)
            except Exception as exc:  # noqa: BLE001
                log.exception("Could not move %s into the library", youtube_id)
                self._on_failure(video_id, DownloadFailed(ErrorKind.TRANSIENT, f"Could not move file into library: {exc}"))
        finally:
            self._finish(video_id, staging)

    def _update_progress(self, video_id: int, data: dict) -> None:
        item = self.active.get(video_id)
        if item is None:
            return
        for key in ("stage", "percent", "speed", "eta", "detail"):
            setattr(item, key, data.get(key))
        self.notifier.progress(video_id, item)

    def _on_success(self, video_id: int, info: VideoInfo, media: Path, extras: list[Path], opts: dict) -> None:
        with self.session_factory() as session:
            video = session.get(Video, video_id)
            if video is None or video_id in self._cancelled:
                return
            mapping = video.mapping
            before = (video.display_title, video.artist, video.song)
            _apply_info(video, info, mapping.type)
            video.attempts = 0
            video.error = None
            video.next_retry_at = None
            organizer.place(session, self.config.media_root, mapping, video, media, extras, opts["max_filename_length"])
            names_changed = before != (video.display_title, video.artist, video.song)
            mapping_id = mapping.id
        self.cooldown_level = 0
        log.info("Downloaded %s", info.title)
        if names_changed:
            self.notifier.list_changed(mapping_id)
        self.notifier.video_changed(video_id, mapping_id)

    def _on_failure(self, video_id: int, err: DownloadFailed) -> None:
        with self.session_factory() as session:
            video = session.get(Video, video_id)
            if video is None:
                return
            mapping = video.mapping
            values = settings_mod.get_all(session)
            video.error = err.message
            if err.kind == ErrorKind.RATE_LIMIT:
                video.status = VideoStatus.PENDING
            elif err.kind == ErrorKind.PERMANENT:
                video.status = VideoStatus.UNAVAILABLE
                video.next_retry_at = None
            else:
                video.attempts += 1
                delay = backoff_delay(video.attempts, values["retry_backoff_minutes"])
                video.status = VideoStatus.FAILED
                video.next_retry_at = self._now() + delay if delay else None
            session.commit()
            log.warning("Download of %s failed (%s): %s", video.youtube_id, err.kind, err.message)
            if err.kind == ErrorKind.PERMANENT:
                organizer.apply(session, self.config.media_root, mapping, values["max_filename_length"])
            mapping_id = mapping.id
        if err.kind == ErrorKind.RATE_LIMIT:
            self.enter_cooldown(err.message)
        if err.kind == ErrorKind.PERMANENT:
            self.notifier.list_changed(mapping_id)
        self.notifier.video_changed(video_id, mapping_id)

    def _finish(self, video_id: int, staging: Path) -> None:
        self.active.pop(video_id, None)
        self._cancelled.discard(video_id)
        shutil.rmtree(staging, ignore_errors=True)
        with self.session_factory() as session:
            values = settings_mod.get_all(session)
        delay = self._rng.uniform(values["min_delay_seconds"], values["max_delay_seconds"])
        self.next_download_at = self._now() + timedelta(seconds=delay)
        self.publish_activity()
        self.kick()
