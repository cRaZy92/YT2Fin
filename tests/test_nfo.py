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


def test_tvshow_nfo_with_show_info():
    root = ET.fromstring(tvshow_nfo("S", plot="About", studio="Chan", premiered="20190305").split("?>", 1)[1])
    assert [root.findtext(t) for t in ("title", "plot", "studio", "premiered", "year")] == [
        "S", "About", "Chan", "2019-03-05", "2019"]


def test_runtime_and_studio_in_episode_and_musicvideo():
    ep = ET.fromstring(episode_nfo(title="T", show_title="S", season=1, episode=1, plot=None, aired=None,
                                   youtube_id="x", runtime=754, studio="Chan").split("\n", 1)[1])
    assert (ep.findtext("runtime"), ep.findtext("studio")) == ("13", "Chan")
    mv = ET.fromstring(musicvideo_nfo(title="S", artist="A", plot=None, premiered=None, youtube_id="y",
                                      runtime=20, studio="Label").split("\n", 1)[1])
    assert (mv.findtext("runtime"), mv.findtext("studio")) == ("1", "Label")
