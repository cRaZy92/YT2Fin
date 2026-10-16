import asyncio
import logging
from datetime import datetime

from app.activity import ActiveItem, ActivitySnapshot
from app.events import Event, EventBus, format_sse
from app.logbuffer import LogBuffer
from app.notify import Notifier
from app.templating import duration, speed, templates


async def test_publish_reaches_subscribers_and_unsubscribe_stops_it():
    bus = EventBus()
    bus.bind(asyncio.get_running_loop())
    q1, q2 = bus.subscribe(), bus.subscribe()
    bus.publish("x", "1")
    assert (await q1.get()).name == "x" and (await q2.get()).data == "1"
    bus.unsubscribe(q1)
    bus.publish("y", "2")
    assert q1.empty() and (await q2.get()).name == "y"


async def test_full_queue_drops_events_instead_of_blocking():
    bus = EventBus(maxsize=1)
    bus.bind(asyncio.get_running_loop())
    q = bus.subscribe()
    bus.publish("a", "1")
    bus.publish("b", "2")
    assert q.qsize() == 1


async def test_publish_threadsafe_from_worker_thread():
    bus = EventBus()
    bus.bind(asyncio.get_running_loop())
    q = bus.subscribe()
    await asyncio.to_thread(bus.publish_threadsafe, "t", "data")
    event = await asyncio.wait_for(q.get(), 1)
    assert event == Event("t", "data")


def test_format_sse_multiline():
    assert format_sse(Event("e", "<a>\n<b>")) == "event: e\ndata: <a>\ndata: <b>\n\n"


async def test_notifier_renders_progress_and_activity():
    bus = EventBus()
    bus.bind(asyncio.get_running_loop())
    q = bus.subscribe()
    notifier = Notifier(bus, templates.env)
    item = ActiveItem(video_id=7, mapping_id=1, title="T", mapping_name="M", stage="downloading",
                      percent=42.5, speed=2048.0, eta=65)
    notifier.progress(7, item)
    event = await q.get()
    assert event.name == "progress-7" and "width: 42.5%" in event.data and "2.0 KiB/s" in event.data
    notifier.activity(ActivitySnapshot(active=[item], queued=3, cooldown_until=datetime(2026, 1, 1),
                                       cooldown_reason="429", disk_free=2 * 1024**3, disk_total=8 * 1024**3,
                                       disk_low=True))
    event = await q.get()
    assert event.name == "activity" and "3 waiting" in event.data and "rate-limited" in event.data
    assert "2.0 GB free of 8.0 GB" in event.data and "disk low" in event.data
    notifier.sync_status(1, "<b>bad</b>")
    assert (await q.get()).data == "&lt;b&gt;bad&lt;/b&gt;"
    notifier.video_changed(7, 1)
    assert [(await q.get()).name, (await q.get()).name] == ["video-7", "mapping-1"]


def test_log_buffer_captures_and_notifies():
    buffer = LogBuffer(capacity=2)
    seen = []
    buffer.listener = seen.append
    logger = logging.getLogger("test-buffer")
    logger.addHandler(buffer)
    logger.setLevel(logging.INFO)
    for i in range(3):
        logger.info("line %s", i)
    logger.removeHandler(buffer)
    assert list(buffer.lines) == ["line 1", "line 2"]
    assert seen == ["line 0", "line 1", "line 2"]


def test_filters():
    assert speed(None) == "" and speed(512) == "512.0 B/s" and speed(3 * 1024 * 1024) == "3.0 MiB/s"
    assert duration(None) == "" and duration(65) == "1:05" and duration(3725) == "1:02:05"
