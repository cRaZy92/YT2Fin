from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta

from sqlalchemy import select

from app import settings as settings_mod
from app.models import Mapping
from app.timeutil import utcnow

log = logging.getLogger(__name__)


class Scheduler:
    TICK_SECONDS = 30
    UPDATE_INTERVAL = timedelta(hours=24)

    def __init__(self, session_factory, worker, syncer, updater, *, now=utcnow,
                 rng: random.Random | None = None, sleep=asyncio.sleep) -> None:
        self.session_factory = session_factory
        self.worker = worker
        self.syncer = syncer
        self.updater = updater
        self._now = now
        self._rng = rng or random.Random()
        self._sleep = sleep
        self._last_update_check: datetime | None = None

    def due_mapping_ids(self, session) -> list[int]:
        default_interval = settings_mod.get_all(session)["sync_interval_minutes"]
        now = self._now()
        due = []
        mappings = session.scalars(
            select(Mapping).where(Mapping.enabled.is_(True), Mapping.playlist_url.is_not(None)).order_by(Mapping.id)
        )
        for mapping in mappings:
            interval = timedelta(minutes=mapping.sync_interval_minutes or default_interval)
            if mapping.last_synced_at is None or mapping.last_synced_at + interval <= now:
                due.append(mapping.id)
        return due

    async def tick(self) -> None:
        with self.session_factory() as session:
            values = settings_mod.get_all(session)
        now = self._now()
        if values["ytdlp_auto_update"] and (
            self._last_update_check is None or now - self._last_update_check >= self.UPDATE_INTERVAL
        ):
            self._last_update_check = now
            try:
                await self.updater.check()
            except Exception:
                log.exception("yt-dlp update check failed")
        if getattr(self.worker, "hold_until", None) is not None:
            self.worker.hold_until = None  # startup update check is done (or disabled): start downloading
            self.worker.kick()
        if self.worker.is_blocked():
            return
        with self.session_factory() as session:
            due = self.due_mapping_ids(session)
        for index, mapping_id in enumerate(due):
            if self.worker.is_blocked():
                break
            if index > 0 and values["sync_jitter_seconds"]:
                await self._sleep(self._rng.uniform(0, values["sync_jitter_seconds"]))
            await self.syncer.sync(mapping_id)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Scheduler tick failed")
            await self._sleep(self.TICK_SECONDS)
