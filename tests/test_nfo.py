from xml.etree import ElementTree as ET

from app.nfo import episode_nfo, format_date, musicvideo_nfo, tvshow_nfo


def test_format_date():
    assert format_date("20240131") == "2024-01-31"
    assert format_date(None) is None
    assert format_date("2024") is None


def test_episode_nfo_fields():
    xml = episode_nfo(title="A & B", show_title="Show", season=2, episode=3, plot="Line1\nLine2\x07",
                      aired="20240131", youtube_id="abc")
    root = ET.fromstring(xml.split("\n", 1)[1])
    assert root.tag == "episodedetails"
    assert root.findtext("title") == "A & B"
    assert root.findtext("showtitle") == "Show"
    assert root.findtext("season") == "2"
    assert root.findtext("episode") == "3"
    assert root.findtext("plot") == "Line1\nLine2"
    assert root.findtext("aired") == "2024-01-31"
    uid = root.find("uniqueid")
    assert uid.get("type") == "youtube" and uid.text == "abc"


def test_missing_values_are_omitted():
    xml = episode_nfo(title="T", show_title="S", season=1, episode=1, plot=None, aired=None, youtube_id="x")
    root = ET.fromstring(xml.split("\n", 1)[1])
    assert root.find("plot") is None
    assert root.find("aired") is None


def test_musicvideo_nfo():
    xml = musicvideo_nfo(title="Song", artist="Artist", plot="p", premiered="20200101", youtube_id="y")
    root = ET.fromstring(xml.split("\n", 1)[1])
    assert root.tag == "musicvideo"
    assert root.findtext("artist") == "Artist"
    assert root.findtext("premiered") == "2020-01-01"
    assert root.findtext("year") == "2020"


def test_tvshow_nfo():
    assert "<title>My Show</title>" in tvshow_nfo("My Show")
