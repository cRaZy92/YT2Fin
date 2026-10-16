import pytest

from app.errors import ErrorKind
from app.models import IgnoredVideo, Mapping, MappingType, Video
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


def test_sync_skips_ignored_but_manual_add_unignores(session, make_mapping):
    mapping = make_mapping()
    mapping.ignored.append(IgnoredVideo(youtube_id="x", title="X"))
    session.commit()
    assert add_entries(session, mapping, [Entry("x", "X")], "sync") == []
    added = add_entries(session, mapping, [Entry("x", "X")], "manual")
    assert [v.youtube_id for v in added] == ["x"] and mapping.ignored == []


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
