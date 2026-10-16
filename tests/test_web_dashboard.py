import asyncio
import base64
import logging
from dataclasses import replace

from fastapi.testclient import TestClient

from app.events import EventBus
from app.main import create_app
from app.models import VideoStatus
from app.web.sse import event_stream

HX = {"HX-Request": "true"}


def test_dashboard_empty(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "No mappings yet" in response.text
    assert 'sse-connect="/events"' in response.text


def test_dashboard_lists_mapping_with_counts(client, make_mapping, add_video):
    mapping = make_mapping("Veritasium", playlist_url="https://www.youtube.com/playlist?list=PL")
    add_video(mapping, "a", status=VideoStatus.DONE)
    add_video(mapping, "b")
    page = client.get("/").text
    assert "Veritasium" in page and "1 done" in page and "1 queued" in page and "Sync now" in page
    card = client.get(f"/mappings/{mapping.id}/card")
    assert card.status_code == 200 and f'hx-trigger="sse:mapping-{mapping.id}"' in card.text
    assert client.get("/mappings/999/card").status_code == 404


def test_healthz_and_static(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/static/app.js").status_code == 200


def test_activity_partial(client):
    response = client.get("/activity")
    assert response.status_code == 200 and "waiting" in response.text


def test_pause_and_resume(client, app):
    assert client.post("/queue/pause", headers=HX).status_code == 204
    assert app.state.worker.paused
    response = client.post("/queue/resume", follow_redirects=False)
    assert response.status_code == 303 and not app.state.worker.paused


def test_logs_page_shows_buffered_lines(client):
    logging.getLogger("test").warning("hello-log-line")
    assert "hello-log-line" in client.get("/logs").text


def test_basic_auth(config):
    app = create_app(replace(config, auth_user="u", auth_password="p"), start_background=False)
    with TestClient(app) as client:
        assert client.get("/").status_code == 401
        assert client.get("/healthz").status_code == 200
        good = base64.b64encode(b"u:p").decode()
        bad = base64.b64encode(b"u:x").decode()
        assert client.get("/", headers={"Authorization": f"Basic {good}"}).status_code == 200
        assert client.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401


async def test_event_stream_formats_and_unsubscribes():
    bus = EventBus()
    bus.bind(asyncio.get_running_loop())
    queue = bus.subscribe()

    async def connected():
        return False

    stream = event_stream(bus, queue, connected)
    assert await stream.__anext__() == "retry: 3000\n\n"
    bus.publish("x", "hi")
    assert await stream.__anext__() == "event: x\ndata: hi\n\n"
    await stream.aclose()
    assert queue not in bus._subscribers


async def test_event_stream_ends_when_server_is_stopping():
    bus = EventBus()
    bus.bind(asyncio.get_running_loop())
    queue = bus.subscribe()
    stopping = False

    async def connected():
        return False

    stream = event_stream(bus, queue, connected, lambda: stopping)
    assert await stream.__anext__() == "retry: 3000\n\n"
    stopping = True
    chunks = [chunk async for chunk in stream]  # must terminate on its own
    assert all(c.startswith(":") for c in chunks)
    assert queue not in bus._subscribers


def test_server_has_graceful_shutdown_timeout(monkeypatch, config):
    import app.__main__ as entry

    captured = {}

    class FakeServer:
        def __init__(self, cfg):
            captured["cfg"] = cfg

        def run(self):
            pass

    monkeypatch.setattr(entry, "load_config", lambda: config)
    monkeypatch.setattr(entry.uvicorn, "Server", FakeServer)
    try:
        entry.main()
    except SystemExit:
        pass
    assert 0 < captured["cfg"].timeout_graceful_shutdown <= 5


def test_cross_site_posts_are_rejected(client, app):
    assert client.post("/queue/pause", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/queue/pause", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert not app.state.worker.paused
    assert client.post("/queue/pause", headers={"Origin": "http://testserver", **HX}).status_code == 204
    assert client.post("/queue/resume", headers={"Sec-Fetch-Site": "same-origin", **HX}).status_code == 204
    assert client.get("/", headers={"Origin": "http://evil.example"}).status_code == 200


def test_activity_banner_shows_media_problem(client, config):
    config.media_root.chmod(0o555)
    try:
        assert "not writable" in client.get("/activity").text
    finally:
        config.media_root.chmod(0o755)


def test_card_and_detail_show_stats(client, make_mapping, add_video):
    mapping = make_mapping("Stats")
    add_video(mapping, "a", status=VideoStatus.DONE, file_size=3 * 1024**3, duration=2 * 3600 + 5 * 60)
    card = client.get(f"/mappings/{mapping.id}/card").text
    assert "3.0 GB" in card and "2 h 5 m" in card
    detail = client.get(f"/mappings/{mapping.id}").text
    assert "3.0 GB" in detail and "2 h 5 m" in detail and "Last download" in detail
