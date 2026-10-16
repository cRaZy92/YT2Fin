from pathlib import Path

from sqlalchemy import select

from app.models import Mapping
from tests.conftest import wait_until

HX = {"HX-Request": "true"}
PLAYLIST = "https://www.youtube.com/playlist?list=PL1"


class FakeSyncer:
    def __init__(self):
        self.synced = []
        self.running = set()

    async def sync(self, mapping_id):
        self.synced.append(mapping_id)


def form(**kw):
    data = {"name": "Veritasium", "type": "show", "folder": "shows/Veritasium", "playlist_url": PLAYLIST,
            "show_title": "", "enabled": "on", "sync_interval_minutes": ""}
    data.update(kw)
    return data


def test_new_form_renders(client):
    page = client.get("/mappings/new").text
    assert "New mapping" in page and 'name="ov_max_height"' in page and "Use global (1080)" in page


def test_create_mapping_redirects_and_triggers_sync(client, app, session, config):
    app.state.syncer = FakeSyncer()
    response = client.post("/mappings", data=form(), follow_redirects=False)
    assert response.status_code == 303
    mapping = session.scalars(select(Mapping)).one()
    assert response.headers["location"] == f"/mappings/{mapping.id}"
    assert mapping.show_title == "Veritasium" and mapping.enabled
    assert (config.media_root / "shows" / "Veritasium").is_dir()
    wait_until(lambda: app.state.syncer.synced == [mapping.id])


def test_create_manual_mapping_with_override(client, app, session):
    app.state.syncer = FakeSyncer()
    client.post("/mappings", data=form(name="Mix", type="music_video", folder="music", playlist_url="",
                                       ov_max_height="720"), follow_redirects=False)
    mapping = session.scalars(select(Mapping)).one()
    assert mapping.playlist_url is None and mapping.overrides == {"max_height": 720} and mapping.show_title is None
    assert app.state.syncer.synced == []


def test_create_mapping_errors_rerender_form(client, make_mapping, config):
    response = client.post("/mappings", data=form(folder="../../etc"))
    assert response.status_code == 400 and "must be a folder inside" in response.text
    assert 'value="Veritasium"' in response.text
    make_mapping("Existing", folder=str(config.media_root / "shows"))
    response = client.post("/mappings", data=form())
    assert response.status_code == 400 and "overlaps" in response.text
    response = client.post("/mappings", data=form(sync_interval_minutes="abc"))
    assert response.status_code == 400 and "whole number" in response.text


def test_edit_mapping(client, app, session, config):
    app.state.syncer = FakeSyncer()
    client.post("/mappings", data=form(), follow_redirects=False)
    mapping = session.scalars(select(Mapping)).one()
    page = client.get(f"/mappings/{mapping.id}/edit").text
    assert 'value="shows/Veritasium"' in page
    response = client.post(f"/mappings/{mapping.id}/edit", data=form(show_title="Veri", enabled=""),
                           follow_redirects=False)
    assert response.status_code == 303
    session.expire_all()
    mapping = session.get(Mapping, mapping.id)
    assert mapping.show_title == "Veri" and not mapping.enabled


def test_delete_mapping_with_files(client, app, session, config):
    app.state.syncer = FakeSyncer()
    client.post("/mappings", data=form(), follow_redirects=False)
    mapping = session.scalars(select(Mapping)).one()
    response = client.post(f"/mappings/{mapping.id}/delete", data={"delete_files": "on"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert not (config.media_root / "shows" / "Veritasium").exists()
    session.expire_all()
    assert session.scalars(select(Mapping)).all() == []


def test_sync_now_and_cooldown(client, app, make_mapping):
    app.state.syncer = FakeSyncer()
    mapping = make_mapping(playlist_url=PLAYLIST)
    assert client.post(f"/mappings/{mapping.id}/sync", headers=HX).status_code == 204
    wait_until(lambda: app.state.syncer.synced == [mapping.id])
    app.state.worker.enter_cooldown("429")
    response = client.post(f"/mappings/{mapping.id}/sync", headers=HX)
    assert response.status_code == 409 and "rate-limit" in response.text


def test_folder_picker(client, config):
    (config.media_root / "a" / "b").mkdir(parents=True)
    (config.media_root / ".hidden").mkdir()
    root = client.get("/folders").text
    assert ">📁 a<" in root.replace("\n", "") and ".hidden" not in root
    assert "📁 b" in client.get("/folders", params={"path": "a"}).text
    assert client.get("/folders", params={"path": "../.."}).status_code == 400
    assert client.get("/folders", params={"path": "missing"}).status_code == 404
    response = client.post("/folders", data={"path": "a", "new_folder_name": "new: one"})
    assert response.status_code == 200 and (config.media_root / "a" / "new one").is_dir()


def test_channel_mapping_with_shorts_checkbox(client, app, session):
    app.state.syncer = FakeSyncer()
    page = client.get("/mappings/new").text
    assert "playlist or channel" in page and 'name="include_shorts"' in page
    client.post("/mappings", data=form(playlist_url="https://www.youtube.com/@Veritasium", include_shorts="on"),
                follow_redirects=False)
    mapping = session.scalars(select(Mapping)).one()
    assert mapping.include_shorts is True
    detail = client.get(f"/mappings/{mapping.id}").text
    assert "Channel:" in detail and "including Shorts" in detail
