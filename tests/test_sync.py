import pytest

from app.errors import ErrorKind
from app.models import Mapping, MappingType, Video, VideoStatus
from app.services import ServiceError
from app.sync import Syncer, add_entries
from app.ytdlp_client import DownloadFailed, Entry

URL = "https://www.youtube.com/playlist?list=PL1"


def test_add_entries_is_additive_and_appends(session, make_mapping, add_video):
    mapping = make_mapping()
    add_video(mapping, "old")
    added = add_entries(session, mapping, [Entry("old", "Old"), Entry("new1", "N1"), Entry("new2", "N2")], "sync")
    assert [v.youtube_id for v in added] == ["new1", "new2"]
    assert [(v.youtube_id, v.position) for v in sorted(mapping.videos, key=lambda v: v.position)] == [
        ("old", 1), ("new1", 2), ("new2", 3)]
    assert all(v.source == "sync" for v in added)


def test_add_entries_dedupes_within_batch(session, make_mapping):
    mapping = make_mapping()
    added = add_entries(session, mapping, [Entry("a", "A"), Entry("a", "A again")], "sync")
    assert len(added) == 1 and len(mapping.videos) == 1


def test_sync_skips_deleted_videos_but_a_manual_add_restores_them(session, make_mapping, add_video):
    mapping = make_mapping()
    add_video(mapping, "a")
    ghost = add_video(mapping, "x", status=VideoStatus.DELETED)
    add_video(mapping, "b")
    assert add_entries(session, mapping, [Entry("x", "X")], "sync") == []
    assert add_entries(session, mapping, [Entry("x", "X")], "manual") == [ghost]
    assert (ghost.status, ghost.position, len(mapping.videos)) == (VideoStatus.PENDING, 2, 3)


def test_music_entries_get_parsed_artist_song(session, make_mapping):
    mapping = make_mapping("Music", type=MappingType.MUSIC_VIDEO)
    [video] = add_entries(session, mapping, [Entry("m", "Queen - Innuendo (Official Video)", "Queen Official")], "sync")
    assert (video.artist, video.song) == ("Queen", "Innuendo")


async def test_syncer_adds_videos_and_notifies(session, session_factory, config, notifier, fake_worker, make_mapping):
    mapping = make_mapping(playlist_url=URL)
    syncer = Syncer(session_factory, config, fake_worker, notifier,
                    fetch=lambda url, opts, cookie, include_shorts: [Entry("a", "A"), Entry("b", "B")])
    assert await syncer.sync(mapping.id) == 2
    session.expire_all()
    reloaded = session.get(Mapping, mapping.id)
    assert len(reloaded.videos) == 2 and reloaded.last_synced_at is not None and reloaded.last_sync_error is None
    assert fake_worker.kicks == 1
    assert ("list_changed", (mapping.id,)) in notifier.calls
    assert notifier.calls[-2] == ("sync_status", (mapping.id, "2 new video(s)"))


async def test_syncer_reverses_a_newest_first_playlist(session, session_factory, config, notifier, fake_worker,
                                                       make_mapping):
    mapping = make_mapping(playlist_url=URL, reverse_order=True)
    playlist = [Entry("ep2", "Ep 2"), Entry("ep1", "Ep 1")]  # newest first
    syncer = Syncer(session_factory, config, fake_worker, notifier,
                    fetch=lambda url, opts, cookie, include_shorts: list(playlist))
    await syncer.sync(mapping.id)
    playlist[:0] = [Entry("ep4", "Ep 4"), Entry("ep3", "Ep 3")]  # two new uploads at the top
    assert await syncer.sync(mapping.id) == 2
    session.expire_all()
    videos = session.get(Mapping, mapping.id).videos
    assert [(v.youtube_id, v.position) for v in videos] == [("ep1", 1), ("ep2", 2), ("ep3", 3), ("ep4", 4)]


async def test_syncer_records_failure_and_cools_down_on_rate_limit(session, session_factory, config, notifier,
                                                                  fake_worker, make_mapping):
    mapping = make_mapping(playlist_url=URL)

    def fetch(url, opts, cookie, include_shorts):
        raise DownloadFailed(ErrorKind.RATE_LIMIT, "HTTP Error 429")

    syncer = Syncer(session_factory, config, fake_worker, notifier, fetch=fetch)
    assert await syncer.sync(mapping.id) is None
    session.expire_all()
    reloaded = session.get(Mapping, mapping.id)
    assert reloaded.last_sync_error == "HTTP Error 429" and reloaded.last_synced_at is not None
    assert fake_worker.cooldowns == ["HTTP Error 429"]
    assert not syncer.running


async def test_syncer_ignores_manual_mappings(session_factory, config, notifier, fake_worker, make_mapping):
    mapping = make_mapping()
    syncer = Syncer(session_factory, config, fake_worker, notifier, fetch=lambda *a: pytest.fail("no fetch"))
    assert await syncer.sync(mapping.id) is None


async def test_add_url(session, session_factory, config, notifier, fake_worker, make_mapping):
    mapping = make_mapping()
    syncer = Syncer(session_factory, config, fake_worker, notifier, fetch=lambda url, opts, cookie, include_shorts: [Entry("v", "V")])
    assert await syncer.add_url(mapping.id, " https://youtu.be/v ") == 1
    session.expire_all()
    assert session.get(Mapping, mapping.id).videos[0].source == "manual"
    with pytest.raises(ServiceError):
        await syncer.add_url(mapping.id, "not a url")


async def test_syncer_passes_include_shorts_to_fetch(session, session_factory, config, notifier, fake_worker,
                                                   make_mapping):
    mapping = make_mapping(playlist_url="https://www.youtube.com/@Chan", include_shorts=True)
    seen = {}

    def fetch(url, opts, cookie, include_shorts):
        seen["args"] = (url, include_shorts)
        return [Entry("a", "A")]

    syncer = Syncer(session_factory, config, fake_worker, notifier, fetch=fetch)
    assert await syncer.sync(mapping.id) == 1
    assert seen["args"] == ("https://www.youtube.com/@Chan", True)
    assert await syncer.add_url(mapping.id, "https://www.youtube.com/@Other") == 0
    assert seen["args"] == ("https://www.youtube.com/@Other", True)


def test_title_filter_only_adds_matching_videos_on_sync(session, make_mapping):
    mapping = make_mapping(title_filter="photos with disturbing backstories")
    entries = [Entry("p1", "Photos With Disturbing Backstories | Episode 1"),
               Entry("c1", "Cases With The Most INSANE Twists | Episode 1"),
               Entry("p2", "PHOTOS WITH DISTURBING BACKSTORIES | Episode 2")]
    added = add_entries(session, mapping, entries, "sync")
    assert [v.youtube_id for v in added] == ["p1", "p2"]


def test_manual_add_ignores_the_title_filter(session, make_mapping):
    mapping = make_mapping(title_filter="photos")
    added = add_entries(session, mapping, [Entry("c1", "Cases With Twists")], "manual")
    assert [v.youtube_id for v in added] == ["c1"]


def test_videos_keep_their_original_order_number(session, config, make_mapping):
    from app import services

    mapping = make_mapping()
    added = add_entries(session, mapping, [Entry("a", "A"), Entry("b", "B"), Entry("c", "C")], "sync")
    assert [v.original_position for v in added] == [1, 2, 3]
    a, b, c = added
    services.reorder(session, config.media_root, mapping, [c.id, a.id, b.id])
    services.delete_video(session, config.media_root, a)
    [d] = add_entries(session, mapping, [Entry("d", "D")], "manual")
    assert {v.youtube_id: v.original_position for v in mapping.videos} == {"a": 1, "b": 2, "c": 3, "d": 4}


JPG = b"\xff\xd8\xff\xe0 jpeg"


async def test_sync_reads_show_info_and_downloads_channel_art_once_a_week(
        session, session_factory, config, notifier, fake_worker, make_mapping):
    from datetime import timedelta
    from pathlib import Path

    from app.ytdlp_client import ShowInfo

    mapping = make_mapping(playlist_url=URL)
    shows, images = [], []
    info = ShowInfo("About the show", "Chan", {"poster": "https://img/avatar", "fanart": "https://img/art"})
    syncer = Syncer(session_factory, config, fake_worker, notifier, fetch=lambda *a: [],
                    fetch_show=lambda *a: shows.append(a[0]) or info,
                    fetch_image=lambda url: images.append(url) or JPG)
    await syncer.sync(mapping.id)
    folder = Path(mapping.folder)
    assert (folder / "poster.jpg").read_bytes() == JPG and (folder / "fanart.jpg").read_bytes() == JPG
    nfo = (folder / "tvshow.nfo").read_text()
    assert "<plot>About the show</plot>" in nfo and "<studio>Chan</studio>" in nfo

    await syncer.sync(mapping.id)  # checked recently: no second fetch
    assert shows == [URL] and len(images) == 2
    with session_factory() as s:
        m = s.get(Mapping, mapping.id)
        m.show_info_checked_at -= timedelta(days=8)
        s.commit()
    await syncer.sync(mapping.id)  # stale: info read again, but unchanged images aren't re-downloaded
    assert shows == [URL, URL] and len(images) == 2
    await syncer.refresh_show_info(mapping.id)  # explicit refresh re-downloads
    assert len(images) == 4


async def test_show_info_failure_does_not_fail_the_sync(session, session_factory, config, notifier, fake_worker,
                                                        make_mapping):
    def failing(*a):
        raise DownloadFailed(ErrorKind.TRANSIENT, "nope")

    mapping = make_mapping(playlist_url=URL)
    syncer = Syncer(session_factory, config, fake_worker, notifier, fetch=lambda *a: [Entry("a", "A")],
                    fetch_show=failing)
    assert await syncer.sync(mapping.id) == 1
    session.expire_all()
    assert session.get(Mapping, mapping.id).show_info_checked_at is None  # retried next time
