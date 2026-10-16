from pathlib import Path

import pytest
import yt_dlp

from app import settings as settings_mod
from app import ytdlp_client
from app.errors import ErrorKind
from app.ytdlp_client import Cancelled, DownloadFailed


def opts_for(**overrides):
    values = dict(settings_mod.DEFAULTS)
    values.update(overrides)
    return values


def test_build_format_any_codec():
    assert ytdlp_client.build_format(1080, "any") == "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"


def test_build_format_preferred_codec_first():
    fmt = ytdlp_client.build_format(720, "h264")
    assert fmt.startswith("bv*[height<=720][vcodec^=avc1]+ba/")
    assert fmt.endswith("/bv*+ba/b")


def test_download_opts_defaults(tmp_path):
    opts = ytdlp_client.download_opts(opts_for(), tmp_path, None, lambda d: None, lambda d: None)
    keys = [pp["key"] for pp in opts["postprocessors"]]
    assert keys == ["FFmpegThumbnailsConvertor", "FFmpegVideoRemuxer", "FFmpegMetadata"]
    assert opts["outtmpl"] == str(tmp_path / "media.%(ext)s")
    assert opts["merge_output_format"] == "mkv"
    assert opts["writethumbnail"] is True
    assert opts["sleep_interval_requests"] == 1.0
    assert "ratelimit" not in opts and "cookiefile" not in opts and "writesubtitles" not in opts


def test_download_opts_all_features(tmp_path):
    cookie = tmp_path / "cookies.txt"
    opts = ytdlp_client.download_opts(
        opts_for(subtitles="sidecar", subtitle_langs="en, de", sponsorblock="remove", ratelimit_kbps=500,
                 sleep_interval=5, max_sleep_interval=10, container="mp4"),
        tmp_path, cookie, lambda d: None, lambda d: None)
    keys = [pp["key"] for pp in opts["postprocessors"]]
    assert "FFmpegSubtitlesConvertor" in keys and "SponsorBlock" in keys and "ModifyChapters" in keys
    modify = next(pp for pp in opts["postprocessors"] if pp["key"] == "ModifyChapters")
    assert modify["remove_sponsor_segments"] == {"sponsor", "selfpromo", "interaction"}
    assert opts["subtitleslangs"] == ["en", "de"]
    assert opts["ratelimit"] == 500 * 1024
    assert opts["sleep_interval"] == 5 and opts["max_sleep_interval"] == 10
    assert opts["cookiefile"] == str(cookie)
    assert opts["merge_output_format"] == "mp4"


def test_download_opts_embed_subtitles(tmp_path):
    opts = ytdlp_client.download_opts(opts_for(subtitles="embed"), tmp_path, None, lambda d: None, lambda d: None)
    assert opts["postprocessors"][-1]["key"] == "FFmpegEmbedSubtitle"


def test_entries_single_video():
    entries = ytdlp_client.entries_from_info({"id": "abc", "title": "T", "channel": "C"})
    assert [(e.youtube_id, e.title, e.channel) for e in entries] == [("abc", "T", "C")]


def test_entries_skip_placeholders_and_tabs():
    info = {"_type": "playlist", "entries": [
        {"id": "a", "title": "A", "uploader": "U"},
        None,
        {"id": "p", "title": "[Private video]"},
        {"id": "d", "title": "[Deleted video]"},
        {"id": "tab", "title": "Videos", "ie_key": "YoutubeTab", "_type": "url"},
        {"id": "b", "title": None},
    ]}
    entries = ytdlp_client.entries_from_info(info)
    assert [(e.youtube_id, e.title, e.channel) for e in entries] == [("a", "A", "U"), ("b", "b", None)]


def test_collect_outputs(tmp_path):
    (tmp_path / "media.mkv").write_text("x" * 10)
    (tmp_path / "media.jpg").write_text("t")
    (tmp_path / "media.en.srt").write_text("s")
    (tmp_path / "media.f137.mp4.part").write_text("p")
    media, extras = ytdlp_client.collect_outputs(tmp_path)
    assert media == tmp_path / "media.mkv"
    assert sorted(p.name for p in extras) == ["media.en.srt", "media.jpg"]


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL; behaviour is set per test via class attributes."""
    behaviour = "ok"

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download):
        staging = Path(self.opts["outtmpl"]).parent
        if FakeYDL.behaviour == "private":
            raise yt_dlp.utils.DownloadError("ERROR: [youtube] abc: Private video")
        for hook in self.opts["progress_hooks"]:
            hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100, "speed": 10.0, "eta": 5})
        (staging / "media.mkv").write_text("video")
        (staging / "media.jpg").write_text("thumb")
        return {"id": "abc", "title": "Title", "channel": "Chan", "upload_date": "20240101", "duration": 61.0,
                "description": "D", "thumbnail": "http://t", "artists": ["A1", "A2"], "track": "Song"}


@pytest.fixture
def fake_ydl(monkeypatch):
    FakeYDL.behaviour = "ok"
    monkeypatch.setattr(ytdlp_client.yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


def test_download_success(fake_ydl, tmp_path):
    events = []
    info, media, extras = ytdlp_client.download("abc", opts_for(), tmp_path / "stage", None, events.append, lambda: False)
    assert media.name == "media.mkv" and [p.name for p in extras] == ["media.jpg"]
    assert info.title == "Title" and info.artist == "A1, A2" and info.track == "Song" and info.duration == 61
    assert events[0] == {"stage": "downloading", "percent": 50.0, "speed": 10.0, "eta": 5, "detail": None}


def test_download_error_is_classified(fake_ydl, tmp_path):
    fake_ydl.behaviour = "private"
    with pytest.raises(DownloadFailed) as exc:
        ytdlp_client.download("abc", opts_for(), tmp_path / "stage", None, lambda d: None, lambda: False)
    assert exc.value.kind == ErrorKind.PERMANENT
    assert exc.value.message == "[youtube] abc: Private video"


def test_download_cancelled(fake_ydl, tmp_path):
    with pytest.raises(Cancelled):
        ytdlp_client.download("abc", opts_for(), tmp_path / "stage", None, lambda d: None, lambda: True)


@pytest.mark.parametrize("url, expected", [
    ("https://www.youtube.com/@Veritasium", True),
    ("https://youtube.com/@Veritasium/videos", True),
    ("https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA", True),
    ("https://www.youtube.com/c/veritasium/shorts?view=0", True),
    ("https://www.youtube.com/user/1veritasium", True),
    ("https://m.youtube.com/@Veritasium/featured", True),
    ("https://www.youtube.com/playlist?list=PL123", False),
    ("https://www.youtube.com/watch?v=abc&list=PL123", False),
    ("https://youtu.be/abc", False),
])
def test_is_channel_url(url, expected):
    assert ytdlp_client.is_channel_url(url) is expected


def test_channel_tab_urls_normalise_to_videos_and_optional_shorts():
    assert ytdlp_client.channel_tab_urls("https://www.youtube.com/@Veritasium/featured?x=1", False) == [
        "https://www.youtube.com/@Veritasium/videos"]
    assert ytdlp_client.channel_tab_urls("https://youtube.com/channel/UCabc/", True) == [
        "https://youtube.com/channel/UCabc/videos", "https://youtube.com/channel/UCabc/shorts"]


def test_fetch_source_entries_channel_is_oldest_first_and_skips_shorts_by_default(monkeypatch):
    calls = []

    def fake_fetch(url, settings, cookiefile):
        calls.append(url)
        tab = url.rsplit("/", 1)[1]
        return {"videos": [ytdlp_client.Entry("v3", "V3"), ytdlp_client.Entry("v2", "V2"), ytdlp_client.Entry("v1", "V1")],
                "shorts": [ytdlp_client.Entry("s2", "S2"), ytdlp_client.Entry("s1", "S1")]}[tab]

    monkeypatch.setattr(ytdlp_client, "fetch_entries", fake_fetch)
    entries = ytdlp_client.fetch_source_entries("https://www.youtube.com/@Chan", opts_for(), None, include_shorts=False)
    assert [e.youtube_id for e in entries] == ["v1", "v2", "v3"]
    assert calls == ["https://www.youtube.com/@Chan/videos"]
    entries = ytdlp_client.fetch_source_entries("https://www.youtube.com/@Chan", opts_for(), None, include_shorts=True)
    assert [e.youtube_id for e in entries] == ["v1", "v2", "v3", "s1", "s2"]


def test_fetch_source_entries_tolerates_channel_without_shorts_tab(monkeypatch):
    def fake_fetch(url, settings, cookiefile):
        if url.endswith("/shorts"):
            raise DownloadFailed(ErrorKind.TRANSIENT, "[youtube:tab] @Chan: This channel does not have a shorts tab")
        return [ytdlp_client.Entry("v1", "V1")]

    monkeypatch.setattr(ytdlp_client, "fetch_entries", fake_fetch)
    entries = ytdlp_client.fetch_source_entries("https://www.youtube.com/@Chan", opts_for(), None, include_shorts=True)
    assert [e.youtube_id for e in entries] == ["v1"]


def test_fetch_source_entries_playlist_keeps_playlist_order(monkeypatch):
    monkeypatch.setattr(ytdlp_client, "fetch_entries",
                        lambda url, settings, cookiefile: [ytdlp_client.Entry("b", "B"), ytdlp_client.Entry("a", "A")])
    entries = ytdlp_client.fetch_source_entries("https://www.youtube.com/playlist?list=PL", opts_for(), None,
                                                include_shorts=False)
    assert [e.youtube_id for e in entries] == ["b", "a"]


def test_missing_shorts_tab_is_not_logged_as_an_error(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="yt_dlp"):
        ytdlp_client._Logger().error("ERROR: [youtube:tab] @jawed: This channel does not have a shorts tab")
        ytdlp_client._Logger().error("ERROR: real problem")
    assert [r.levelname for r in caplog.records] == ["INFO", "ERROR"]


def test_download_opts_skip_streams_that_are_live_right_now(tmp_path):
    opts = ytdlp_client.download_opts(opts_for(), tmp_path, None, lambda d: None, lambda d: None)
    assert opts["match_filter"]({"is_live": True}, incomplete=False)  # a message = skip
    assert opts["match_filter"]({"is_live": False}, incomplete=False) is None


def test_download_of_live_stream_reports_not_yet(fake_ydl, tmp_path, monkeypatch):
    def live_extract(self, url, download):
        return {"id": "abc", "title": "Live", "live_status": "is_live"}

    monkeypatch.setattr(FakeYDL, "extract_info", live_extract)
    with pytest.raises(DownloadFailed) as exc:
        ytdlp_client.download("abc", opts_for(), tmp_path / "stage", None, lambda d: None, lambda: False)
    assert exc.value.kind == ErrorKind.NOT_YET
