import pytest

from app import preview, settings as settings_mod
from app.models import MappingType
from app.services import MappingInput, ServiceError
from app.ytdlp_client import DownloadFailed, Entry, VideoInfo
from app.errors import ErrorKind

ENTRIES = [
    Entry("p1", "Photos With Disturbing Backstories | Episode 1"),
    Entry("c1", "Cases With The Most INSANE Twists | Episode 1"),
    Entry("p2", "Photos With Disturbing Backstories | Episode 2"),
    Entry("p3", "Photos With Disturbing Backstories | Episode 3"),
    Entry("p4", "Photos With Disturbing Backstories | Episode 4"),
]


def info(yid):
    return VideoInfo(youtube_id=yid, title=yid, channel="EWU", upload_date=None, duration=None, thumbnail_url=None,
                     artist=None, track=None,
                     description=f"Story of {yid}.\n\nSponsored by Acme: https://acme.example")


def show_input(**kw):
    data = dict(name="EWU Photos", type=MappingType.SHOW, folder="shows/EWU",
                playlist_url="https://www.youtube.com/@EWUStorytime", title_filter="photos with disturbing")
    data.update(kw)
    return MappingInput(**data)


@pytest.fixture
def fetched(monkeypatch):
    calls = {"entries": [], "videos": []}

    def fake_entries(url, opts, cookie, include_shorts):
        calls["entries"].append((url, include_shorts))
        return ENTRIES

    def fake_video(youtube_id, opts, cookie):
        calls["videos"].append(youtube_id)
        return info(youtube_id)

    monkeypatch.setattr(preview, "fetch_source_entries", fake_entries)
    monkeypatch.setattr(preview, "fetch_video_info", fake_video)
    return calls


async def test_preview_marks_filtered_videos_and_names_matches_in_order(fetched):
    result = await preview.build_preview(show_input(), dict(settings_mod.DEFAULTS), None)
    assert (result.total, result.matched) == (5, 4)
    assert [(r.youtube_id, r.included) for r in result.rows] == [
        ("p1", True), ("c1", False), ("p2", True), ("p3", True), ("p4", True)]
    assert result.rows[0].file_name == "Season 01/EWU Photos S01E01 Photos With Disturbing Backstories Episode 1.mkv"
    assert result.rows[2].file_name.startswith("Season 01/EWU Photos S01E02 ")
    assert result.rows[1].file_name is None


async def test_preview_shows_cleaned_descriptions_for_the_first_three_matches(fetched):
    result = await preview.build_preview(show_input(description_mode="cleaned"), dict(settings_mod.DEFAULTS), None)
    assert fetched["videos"] == ["p1", "p2", "p3"]
    sample = result.samples[0]
    assert sample.result == "Story of p1." and "Sponsored by Acme" in sample.original


async def test_preview_music_video_names_use_the_sync_parser(fetched, monkeypatch):
    monkeypatch.setattr(preview, "fetch_source_entries",
                        lambda *a: [Entry("q", "Queen - Innuendo (Official Video)", "Queen Official")])
    result = await preview.build_preview(
        show_input(type=MappingType.MUSIC_VIDEO, title_filter=None, show_title=None), dict(settings_mod.DEFAULTS), None)
    assert result.rows[0].file_name == "Queen - Innuendo.mkv"


async def test_preview_without_url_is_an_error(fetched):
    with pytest.raises(ServiceError, match="playlist or channel URL"):
        await preview.build_preview(show_input(playlist_url=None), dict(settings_mod.DEFAULTS), None)


async def test_preview_reports_youtube_errors(monkeypatch):
    def failing(*args):
        raise DownloadFailed(ErrorKind.PERMANENT, "This playlist does not exist")

    monkeypatch.setattr(preview, "fetch_source_entries", failing)
    with pytest.raises(ServiceError, match="does not exist"):
        await preview.build_preview(show_input(), dict(settings_mod.DEFAULTS), None)


def test_preview_endpoint_renders_the_panel(client, fetched):
    page = client.get("/mappings/new").text
    assert 'hx-post="/mappings/preview"' in page
    response = client.post("/mappings/preview", data={
        "name": "EWU Photos", "type": "show", "folder": "shows/EWU", "playlist_url": "https://www.youtube.com/@EWU",
        "title_filter": "photos with disturbing", "description_mode": "first_paragraph"})
    assert response.status_code == 200
    text = response.text
    assert "5 videos found" in text and "4 match the title filter" in text
    assert "EWU Photos S01E01 Photos With Disturbing Backstories Episode 1.mkv" in text
    assert "Story of p1." in text and "skipped" in text


def test_preview_endpoint_shows_errors_inline(client, fetched):
    response = client.post("/mappings/preview", data={"name": "X", "type": "show", "folder": "x", "playlist_url": ""})
    assert response.status_code == 200 and "playlist or channel URL" in response.text
