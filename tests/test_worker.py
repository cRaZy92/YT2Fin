import asyncio
import random
import threading
from datetime import timedelta
from pathlib import Path

from app.errors import ErrorKind
from app.models import MappingType, Video, VideoStatus
from app.worker import Worker, backoff_delay, cooldown_minutes
from app.ytdlp_client import Cancelled, DownloadFailed, VideoInfo


def make_info(yid, title=None, artist=None, track=None):
    return VideoInfo(youtube_id=yid, title=title or f"Title {yid}", channel="Chan", description="desc",
                     upload_date="20240101", duration=10, thumbnail_url=None, artist=artist, track=track)


def ok_download(info_factory=make_info):
    def download(yid, opts, staging, cookie, on_progress, is_cancelled):
        staging.mkdir(parents=True, exist_ok=True)
        on_progress({"stage": "downloading", "percent": 50.0, "speed": 1.0, "eta": 1, "detail": None})
        (staging / "media.mkv").write_text(f"video {yid}")
        (staging / "media.jpg").write_text("thumb")
        return info_factory(yid), staging / "media.mkv", [staging / "media.jpg"]
    return download


def failing(kind, message="boom"):
    def download(*args):
        raise DownloadFailed(kind, message)
    return download


def make_worker(session_factory, config, notifier, clock, download):
    return Worker(session_factory, config, notifier, download=download, now=clock, rng=random.Random(1))


def reload(session, video):
    session.expire_all()
    return session.get(Video, video.id)


def test_backoff_and_cooldown_math():
    assert backoff_delay(1, [10, 60]) == timedelta(minutes=10)
    assert backoff_delay(2, [10, 60]) == timedelta(minutes=60)
    assert backoff_delay(3, [10, 60]) is None
    assert [cooldown_minutes(n, 60, 300) for n in (1, 2, 3, 4)] == [60, 120, 240, 300]


async def test_downloads_pending_video_then_throttles(session, session_factory, config, notifier, clock,
                                                     make_mapping, add_video):
    mapping = make_mapping()
    video = add_video(mapping, "a")
    worker = make_worker(session_factory, config, notifier, clock, ok_download())
    assert worker.step() == 0.0
    await worker.drain()
    done = reload(session, video)
    assert done.status == VideoStatus.DONE
    assert (Path(mapping.folder) / done.file_path).read_text() == "video a"
    assert done.file_path == "Season 01/My Show S01E01 Title a.mkv"
    assert 30 <= (worker.next_download_at - clock.now).total_seconds() <= 90
    assert "progress" in notifier.names() and "video_changed" in notifier.names()
    assert not (config.staging_dir / str(video.id)).exists()

    add_video(mapping, "b")
    assert worker.step() > 0 and not worker.active
    clock.advance(seconds=91)
    assert worker.step() == 0.0
    await worker.drain()


async def test_transient_failure_backs_off(session, session_factory, config, notifier, clock, make_mapping, add_video):
    video = add_video(make_mapping(), "a")
    worker = make_worker(session_factory, config, notifier, clock, failing(ErrorKind.TRANSIENT))
    worker.step()
    await worker.drain()
    failed = reload(session, video)
    assert failed.status == VideoStatus.FAILED and failed.attempts == 1 and failed.error == "boom"
    assert failed.next_retry_at == clock.now + timedelta(minutes=10)
    clock.advance(seconds=100)
    assert worker.step() > 0 and not worker.active
    clock.advance(minutes=10)
    assert worker.step() == 0.0
    await worker.drain()
    assert reload(session, video).attempts == 2


async def test_permanent_failure_marks_unavailable_and_renumbers(session, session_factory, config, notifier, clock,
                                                                 make_mapping, add_video):
    mapping = make_mapping()
    bad = add_video(mapping, "bad")
    (Path(mapping.folder) / "x.mkv").write_text("good")
    good = add_video(mapping, "good", status=VideoStatus.DONE, file_path="x.mkv")
    worker = make_worker(session_factory, config, notifier, clock, failing(ErrorKind.PERMANENT, "Private video"))
    worker.step()
    await worker.drain()
    assert reload(session, bad).status == VideoStatus.UNAVAILABLE
    assert reload(session, good).file_path == "Season 01/My Show S01E01 Title good.mkv"
    assert ("list_changed", (mapping.id,)) in notifier.calls


async def test_rate_limit_enters_doubling_cooldown(session, session_factory, config, notifier, clock,
                                                  make_mapping, add_video):
    video = add_video(make_mapping(), "a")
    worker = make_worker(session_factory, config, notifier, clock, failing(ErrorKind.RATE_LIMIT, "HTTP Error 429"))
    worker.step()
    await worker.drain()
    assert reload(session, video).status == VideoStatus.PENDING
    assert worker.cooldown_until == clock.now + timedelta(minutes=60)
    assert worker.is_blocked() and worker.step() > 0 and not worker.active
    clock.advance(minutes=61)
    assert not worker.is_blocked()
    worker.step()
    await worker.drain()
    assert worker.cooldown_until == clock.now + timedelta(minutes=120)
    worker.resume()
    assert not worker.is_blocked()
    worker._download = ok_download()
    clock.advance(minutes=2)
    worker.step()
    await worker.drain()
    assert worker.cooldown_level == 0


async def test_pause_blocks_new_downloads(session_factory, config, notifier, clock, make_mapping, add_video):
    add_video(make_mapping(), "a")
    worker = make_worker(session_factory, config, notifier, clock, ok_download())
    worker.pause()
    assert worker.step() == Worker.POLL_SECONDS and not worker.active
    assert worker.snapshot().queued == 1 and worker.snapshot().paused


async def test_restart_pending_runs_on_idle_when_nothing_active(session_factory, config, notifier, clock, make_mapping,
                                                               add_video):
    add_video(make_mapping(), "a")
    worker = make_worker(session_factory, config, notifier, clock, ok_download())
    calls = []
    worker.on_idle = lambda: calls.append(1)
    worker.restart_pending = True
    worker.step()
    assert calls == [1] and not worker.active


async def test_mapping_deleted_mid_download_leaves_no_files(session, session_factory, config, notifier, clock,
                                                            make_mapping, add_video):
    mapping = make_mapping()
    video = add_video(mapping, "a")
    started, release = threading.Event(), threading.Event()
    inner = ok_download()

    def download(*args):
        started.set()
        release.wait(5)
        return inner(*args)

    worker = make_worker(session_factory, config, notifier, clock, download)
    worker.step()
    await asyncio.to_thread(started.wait, 5)
    folder = Path(mapping.folder)
    session.delete(mapping)
    session.commit()
    release.set()
    await worker.drain()
    assert not [p for p in folder.rglob("*") if p.is_file()]
    assert not (config.staging_dir / str(video.id)).exists()
    assert not worker.active


async def test_cancel_stops_download(session, session_factory, config, notifier, clock, make_mapping, add_video):
    video = add_video(make_mapping(), "a")
    started = threading.Event()

    def download(yid, opts, staging, cookie, on_progress, is_cancelled):
        started.set()
        for _ in range(500):
            if is_cancelled():
                raise Cancelled()
            threading.Event().wait(0.01)
        raise AssertionError("never cancelled")

    worker = make_worker(session_factory, config, notifier, clock, download)
    worker.step()
    await asyncio.to_thread(started.wait, 5)
    worker.cancel(video.id)
    await worker.drain()
    assert not worker.active
    assert reload(session, video).status == VideoStatus.DOWNLOADING  # caller (delete) removes the row


async def test_music_metadata_reparsed_unless_user_edited(session, session_factory, config, notifier, clock,
                                                         make_mapping, add_video):
    mapping = make_mapping("Music", type=MappingType.MUSIC_VIDEO)
    auto = add_video(mapping, "auto", artist="Wrong", song="Wrong")
    edited = add_video(mapping, "edited", artist="Mine", song="Mine", user_edited=True)
    info = lambda yid: make_info(yid, title="X - Y", artist="Queen", track=f"Song {yid}")  # noqa: E731
    worker = make_worker(session_factory, config, notifier, clock, ok_download(info))
    for _ in range(2):
        worker.next_download_at = None
        worker.step()
        await worker.drain()
    assert (reload(session, auto).artist, reload(session, auto).song) == ("Queen", "Song auto")
    assert (reload(session, edited).artist, reload(session, edited).song) == ("Mine", "Mine")
    assert (Path(mapping.folder) / "Queen - Song auto.mkv").exists()


async def test_worker_waits_and_reports_when_media_root_is_not_writable(session_factory, config, notifier, clock,
                                                                       make_mapping, add_video):
    add_video(make_mapping(), "a")
    worker = make_worker(session_factory, config, notifier, clock, ok_download())
    config.media_root.chmod(0o555)
    try:
        assert worker.step() == Worker.POLL_SECONDS and not worker.active
        assert "not writable" in worker.snapshot().media_problem
    finally:
        config.media_root.chmod(0o755)
    assert worker.snapshot().media_problem is None
    assert worker.step() == 0.0
    await worker.drain()


async def test_queue_takes_turns_between_mappings(session, session_factory, config, notifier, clock, make_mapping,
                                                  add_video):
    big = make_mapping("Big")
    small = make_mapping("Small")
    for yid in ("b1", "b2", "b3"):
        add_video(big, yid)
    for yid in ("s1", "s2"):
        add_video(small, yid)
    order = []

    def download(yid, *args):
        order.append(yid)
        return ok_download()(yid, *args)

    worker = make_worker(session_factory, config, notifier, clock, download)
    for _ in range(5):
        worker.next_download_at = None
        assert worker.step() == 0.0
        await worker.drain()
    assert order == ["b1", "s1", "b2", "s2", "b3"]


async def test_upcoming_premiere_waits_without_using_retries(session, session_factory, config, notifier, clock,
                                                            make_mapping, add_video):
    video = add_video(make_mapping(), "a")
    worker = make_worker(session_factory, config, notifier, clock, failing(ErrorKind.NOT_YET, "Premieres in 3 hours"))
    worker.step()
    await worker.drain()
    upcoming = reload(session, video)
    assert upcoming.status == VideoStatus.PENDING and upcoming.attempts == 0
    assert upcoming.error == "Premieres in 3 hours"
    assert upcoming.next_retry_at == clock.now + timedelta(minutes=60)
    clock.advance(minutes=5)
    assert worker.step() > 0 and not worker.active
    clock.advance(minutes=60)
    worker._download = ok_download()
    assert worker.step() == 0.0
    await worker.drain()
    assert reload(session, video).status == VideoStatus.DONE


def test_upcoming_status_badge(client, make_mapping, add_video, clock):
    video = add_video(make_mapping(), "a", error="Premieres in 3 hours", next_retry_at=clock.now)
    assert "Upcoming" in client.get(f"/videos/{video.id}/status").text


async def test_worker_pauses_when_disk_space_is_low(session, session_factory, config, notifier, clock, make_mapping,
                                                    add_video, monkeypatch):
    import shutil as _shutil

    from app import settings as settings_mod

    add_video(make_mapping(), "a")
    settings_mod.set_many(session, {"min_free_gb": "5"})
    usage = _shutil.disk_usage(config.media_root)
    monkeypatch.setattr("app.organizer.shutil.disk_usage",
                        lambda path: usage._replace(free=2 * 1024**3))
    worker = make_worker(session_factory, config, notifier, clock, ok_download())
    assert worker.step() == Worker.POLL_SECONDS and not worker.active
    assert "Low disk space" in worker.snapshot().media_problem
    settings_mod.set_many(session, {"min_free_gb": "0"})  # 0 disables the check
    assert worker.snapshot().media_problem is None
    assert worker.step() == 0.0
    await worker.drain()


async def test_cooldown_survives_a_restart(session_factory, config, notifier, clock):
    first = make_worker(session_factory, config, notifier, clock, ok_download())
    first.enter_cooldown("HTTP Error 429")
    first.enter_cooldown("HTTP Error 429")
    restarted = make_worker(session_factory, config, notifier, clock, ok_download())
    assert restarted.cooldown_until == clock.now + timedelta(minutes=120)
    assert restarted.cooldown_level == 2 and restarted.cooldown_reason == "HTTP Error 429"
    assert restarted.is_blocked()
    restarted.resume()
    again = make_worker(session_factory, config, notifier, clock, ok_download())
    assert again.cooldown_until is None and not again.is_blocked()
