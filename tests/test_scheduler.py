import random
from datetime import timedelta

from app import settings as settings_mod
from app.scheduler import Scheduler
from app.updater import Updater

URL = "https://www.youtube.com/playlist?list=PL"


class FakeSyncer:
    def __init__(self):
        self.synced = []

    async def sync(self, mapping_id):
        self.synced.append(mapping_id)
        return 0


class FakeUpdater:
    def __init__(self):
        self.checks = 0

    async def check(self):
        self.checks += 1
        return "ok"


def make_scheduler(session_factory, fake_worker, clock, sleeps):
    async def sleep(seconds):
        sleeps.append(seconds)

    return Scheduler(session_factory, fake_worker, FakeSyncer(), FakeUpdater(), now=clock,
                     rng=random.Random(0), sleep=sleep)


def test_due_mapping_ids(session, session_factory, fake_worker, clock, make_mapping):
    never = make_mapping("Never", playlist_url=URL)
    recent = make_mapping("Recent", playlist_url=URL, last_synced_at=clock.now - timedelta(minutes=10))
    old = make_mapping("Old", playlist_url=URL, last_synced_at=clock.now - timedelta(minutes=400))
    custom = make_mapping("Custom", playlist_url=URL, sync_interval_minutes=5,
                          last_synced_at=clock.now - timedelta(minutes=6))
    make_mapping("Manual")
    make_mapping("Disabled", playlist_url=URL, enabled=False)
    scheduler = make_scheduler(session_factory, fake_worker, clock, [])
    assert scheduler.due_mapping_ids(session) == [never.id, old.id, custom.id]
    assert recent.id not in scheduler.due_mapping_ids(session)


async def test_tick_syncs_due_mappings_with_jitter_between(session, session_factory, fake_worker, clock, make_mapping):
    settings_mod.set_many(session, {"ytdlp_auto_update": False})
    a = make_mapping("A", playlist_url=URL)
    b = make_mapping("B", playlist_url=URL)
    sleeps = []
    scheduler = make_scheduler(session_factory, fake_worker, clock, sleeps)
    await scheduler.tick()
    assert scheduler.syncer.synced == [a.id, b.id]
    assert len(sleeps) == 1 and 0 <= sleeps[0] <= 300


async def test_tick_does_nothing_while_blocked(session_factory, fake_worker, clock, make_mapping):
    make_mapping("A", playlist_url=URL)
    fake_worker.blocked = True
    scheduler = make_scheduler(session_factory, fake_worker, clock, [])
    await scheduler.tick()
    assert scheduler.syncer.synced == []


async def test_update_check_runs_at_start_and_daily(session_factory, fake_worker, clock):
    scheduler = make_scheduler(session_factory, fake_worker, clock, [])
    await scheduler.tick()
    await scheduler.tick()
    assert scheduler.updater.checks == 1
    clock.advance(hours=24)
    await scheduler.tick()
    assert scheduler.updater.checks == 2


async def test_updater_requests_restart_when_new_version_installed(fake_worker):
    async def upgrade():
        return 0, "Successfully installed yt-dlp-2026.1.1"

    updater = Updater(fake_worker, upgrade=upgrade, installed=lambda: "2026.1.1", running=lambda: "2025.12.1")
    result = await updater.check()
    assert fake_worker.restart_pending is True and "2026.1.1" in result and fake_worker.kicks == 1


async def test_updater_up_to_date_and_failure(fake_worker):
    async def ok():
        return 0, ""

    async def broken():
        return 1, "line\nERROR: no network"

    assert "Up to date" in await Updater(fake_worker, upgrade=ok, installed=lambda: "1", running=lambda: "1").check()
    result = await Updater(fake_worker, upgrade=broken, installed=lambda: "1", running=lambda: "1").check()
    assert result == "Update failed: ERROR: no network"
    assert fake_worker.restart_pending is False


async def test_updater_treats_zero_padded_versions_as_equal(fake_worker):
    async def ok():
        return 0, ""

    updater = Updater(fake_worker, upgrade=ok, installed=lambda: "2026.8.19", running=lambda: "2026.08.19")
    assert "Up to date" in await updater.check()
    assert fake_worker.restart_pending is False


async def test_first_update_check_releases_the_startup_hold(session_factory, fake_worker, clock):
    fake_worker.hold_until = clock.now
    scheduler = make_scheduler(session_factory, fake_worker, clock, [])
    await scheduler.tick()
    assert fake_worker.hold_until is None and fake_worker.kicks >= 1


async def test_startup_hold_released_even_when_auto_update_is_off(session, session_factory, fake_worker, clock):
    settings_mod.set_many(session, {"ytdlp_auto_update": False})
    fake_worker.hold_until = clock.now
    scheduler = make_scheduler(session_factory, fake_worker, clock, [])
    await scheduler.tick()
    assert fake_worker.hold_until is None
