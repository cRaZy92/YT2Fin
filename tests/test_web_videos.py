from pathlib import Path

from sqlalchemy import select

from app.models import IgnoredVideo, MappingType, SeasonBreak, Video, VideoStatus
from app.sync import Syncer
from app.ytdlp_client import Entry
from tests.conftest import FakeNotifier, FakeWorker

PLAYLIST = "https://www.youtube.com/playlist?list=PL1"


def done(mapping, add_video, yid, title):
    (Path(mapping.folder) / f"{yid}.mkv").write_text(yid)
    return add_video(mapping, yid, title=title, status=VideoStatus.DONE, file_path=f"{yid}.mkv")


def test_detail_page_lists_videos_with_previews(client, make_mapping, add_video):
    mapping = make_mapping(playlist_url=PLAYLIST)
    add_video(mapping, "a", title="First")
    add_video(mapping, "b", title="Second", status=VideoStatus.FAILED, error="boom")
    page = client.get(f"/mappings/{mapping.id}").text
    assert "First" in page and "Second" in page
    assert "Season 01/My Show S01E01 First.mkv" in page
    assert "S01E02" in page and "boom" in page and "Retry" in page
    assert f'data-sortable="/mappings/{mapping.id}/reorder"' in page
    assert client.get("/mappings/999").status_code == 404


def test_reorder(client, session, make_mapping, add_video):
    mapping = make_mapping()
    a = done(mapping, add_video, "a", "A")
    b = done(mapping, add_video, "b", "B")
    response = client.post(f"/mappings/{mapping.id}/reorder", data={"order": f"{b.id},{a.id}"})
    assert response.status_code == 200 and 'id="video-list"' in response.text
    session.expire_all()
    assert session.get(Video, b.id).position == 1
    bad = client.post(f"/mappings/{mapping.id}/reorder", data={"order": f"{a.id}"})
    assert bad.status_code == 422 and "exactly once" in bad.text


def test_season_toggle(client, session, make_mapping, add_video):
    mapping = make_mapping()
    add_video(mapping, "a")
    b = add_video(mapping, "b")
    response = client.post(f"/videos/{b.id}/season", data={"on": "1"})
    assert response.status_code == 200 and "Season 2" in response.text and "Remove season break" in response.text
    assert session.scalars(select(SeasonBreak)).one().video_id == b.id
    response = client.post(f"/videos/{b.id}/season", data={"on": "0"})
    assert "Season 2" not in response.text


def test_rename_show_video(client, session, make_mapping, add_video):
    mapping = make_mapping()
    video = done(mapping, add_video, "a", "Old")
    assert 'name="title"' in client.get(f"/videos/{video.id}/edit").text
    response = client.post(f"/videos/{video.id}/rename", data={"title": "Shiny"})
    assert response.status_code == 200 and "Shiny" in response.text
    assert (Path(mapping.folder) / "Season 01" / "My Show S01E01 Shiny.mkv").exists()
    assert f'id="video-{video.id}"' in client.get(f"/videos/{video.id}/row").text


def test_rename_music_video_validation(client, make_mapping, add_video):
    mapping = make_mapping("Music", type=MappingType.MUSIC_VIDEO)
    video = add_video(mapping, "m", artist="A", song="S")
    response = client.post(f"/videos/{video.id}/rename", data={"artist": "Queen", "song": ""})
    assert response.status_code == 422
    assert response.headers["HX-Retarget"] == f"#video-{video.id}" and "required" in response.text


def test_delete_with_ignore(client, session, make_mapping, add_video):
    mapping = make_mapping(playlist_url=PLAYLIST)
    video_id = done(mapping, add_video, "a", "A").id
    response = client.post(f"/videos/{video_id}/delete", data={"ignore": "on"})
    assert response.status_code == 200 and "No videos yet" in response.text
    session.expire_all()
    assert session.get(Video, video_id) is None
    assert session.scalars(select(IgnoredVideo)).one().youtube_id == "a"
    page = client.get(f"/mappings/{mapping.id}").text
    assert "Ignored videos (1)" in page
    response = client.post(f"/mappings/{mapping.id}/ignored/a/unignore", follow_redirects=False)
    assert response.status_code == 303
    session.expire_all()
    assert session.scalars(select(IgnoredVideo)).all() == []


def test_retry_and_status(client, session, make_mapping, add_video):
    mapping = make_mapping()
    video = add_video(mapping, "a", status=VideoStatus.FAILED, attempts=2, error="boom")
    assert client.post(f"/videos/{video.id}/retry").status_code == 200
    session.expire_all()
    assert session.get(Video, video.id).status == VideoStatus.PENDING
    assert "Queued" in client.get(f"/videos/{video.id}/status").text


def test_add_url(client, app, config, make_mapping):
    mapping = make_mapping()
    app.state.syncer = Syncer(app.state.session_factory, config, FakeWorker(), FakeNotifier(),
                              fetch=lambda url, opts, cookie, include_shorts: [Entry("new", "New video")])
    response = client.post(f"/mappings/{mapping.id}/videos", data={"url": "https://youtu.be/new"})
    assert response.status_code == 200 and "New video" in response.text and "Added 1 video" in response.text
    response = client.post(f"/mappings/{mapping.id}/videos", data={"url": "nope"})
    assert response.status_code == 422 and "Enter a YouTube" in response.text


def test_edit_row_is_prefilled_with_current_title_and_filename(client, make_mapping, add_video):
    mapping = make_mapping()
    video = done(mapping, add_video, "a", "Photos With Disturbing Backstories | Episode 1")
    page = client.get(f"/videos/{video.id}/edit").text
    assert 'value="Photos With Disturbing Backstories | Episode 1"' in page
    assert "Season 01/My Show S01E01 Photos With Disturbing Backstories   Episode 1.mkv" not in page  # sanitized
    assert "Season 01/My Show S01E01 Photos With Disturbing Backstories Episode 1.mkv" in page


def test_filename_preview_while_typing(client, make_mapping, add_video):
    show = make_mapping()
    video = done(show, add_video, "a", "Old")
    response = client.get(f"/videos/{video.id}/preview", params={"title": "New: title"})
    assert response.status_code == 200 and response.text.strip() == "Season 01/My Show S01E01 New title.mkv"
    music = make_mapping("Music", type=MappingType.MUSIC_VIDEO)
    mv = add_video(music, "m", artist="A", song="S")
    response = client.get(f"/videos/{mv.id}/preview", params={"artist": "Queen", "song": "Innuendo"})
    assert response.text.strip() == "Queen - Innuendo.mkv"


def test_saving_the_unchanged_youtube_title_keeps_it_automatic(client, session, make_mapping, add_video):
    mapping = make_mapping()
    video = done(mapping, add_video, "a", "Original")
    client.post(f"/videos/{video.id}/rename", data={"title": "Original"})
    session.expire_all()
    assert session.get(Video, video.id).custom_title is None


def test_row_shows_original_order_number(client, make_mapping, add_video):
    mapping = make_mapping()
    add_video(mapping, "a", original_position=7)
    assert 'title="Original order: 7"' in client.get(f"/mappings/{mapping.id}/list").text
