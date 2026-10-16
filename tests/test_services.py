from pathlib import Path

import pytest
from sqlalchemy import select

from app import services
from app.models import IgnoredVideo, Mapping, MappingType, Video, VideoStatus
from app.services import MappingInput, ServiceError


def show_input(**kw):
    data = dict(name="Veritasium", type=MappingType.SHOW, folder="shows/Veritasium",
                playlist_url="https://www.youtube.com/playlist?list=PL1")
    data.update(kw)
    return MappingInput(**data)


def put_done(session, mapping, add_video, yid, title):
    folder = Path(mapping.folder)
    (folder / f"{yid}.mkv").write_text(f"media {yid}")
    return add_video(mapping, yid, title=title, status=VideoStatus.DONE, file_path=f"{yid}.mkv")


def test_create_mapping_resolves_relative_folder_and_writes_tvshow(session, config):
    mapping = services.create_mapping(session, config.media_root, show_input())
    folder = (config.media_root / "shows" / "Veritasium").resolve()
    assert mapping.folder == str(folder)
    assert mapping.show_title == "Veritasium"
    assert (folder / "tvshow.nfo").exists()


@pytest.mark.parametrize("folder", ["", "../outside", "/etc", ".", ".ytjs-tmp/x", "shows/../../x"])
def test_create_mapping_rejects_escaping_folders(session, config, folder):
    with pytest.raises(ServiceError):
        services.create_mapping(session, config.media_root, show_input(folder=folder))
    assert session.scalars(select(Mapping)).all() == []
    assert not (config.media_root.parent / "outside").exists()


def test_create_mapping_rejects_symlink_escape(session, config, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (config.media_root / "link").symlink_to(outside)
    with pytest.raises(ServiceError):
        services.create_mapping(session, config.media_root, show_input(folder="link/show"))


def test_create_mapping_rejects_overlap(session, config):
    services.create_mapping(session, config.media_root, show_input())
    for folder in ["shows/Veritasium", "shows", "shows/Veritasium/sub"]:
        with pytest.raises(ServiceError, match="overlaps"):
            services.create_mapping(session, config.media_root, show_input(name="Other", folder=folder))


@pytest.mark.parametrize("kw", [{"name": " "}, {"type": "movie"}, {"playlist_url": "ftp://x"},
                                {"sync_interval_minutes": 1}, {"overrides": {"max_height": "123"}}])
def test_create_mapping_validates_input(session, config, kw):
    with pytest.raises(ValueError):
        services.create_mapping(session, config.media_root, show_input(**kw))


def test_update_mapping_moves_folder_and_renames_show(session, config, add_video):
    mapping = services.create_mapping(session, config.media_root, show_input())
    put_done(session, mapping, add_video, "a", "Ep")
    services.update_mapping(session, config.media_root, mapping,
                            show_input(folder="shows/Veri", show_title="Veri"))
    new = (config.media_root / "shows" / "Veri").resolve()
    assert mapping.folder == str(new)
    assert not (config.media_root / "shows" / "Veritasium").exists()
    assert (new / "Season 01" / "Veri S01E01 Ep.mkv").read_text() == "media a"


def test_update_mapping_rejects_type_change(session, config):
    mapping = services.create_mapping(session, config.media_root, show_input())
    with pytest.raises(ServiceError):
        services.update_mapping(session, config.media_root, mapping, show_input(type=MappingType.MUSIC_VIDEO))


def test_delete_mapping_optionally_deletes_files(session, config):
    keep = services.create_mapping(session, config.media_root, show_input(name="Keep", folder="keep"))
    drop = services.create_mapping(session, config.media_root, show_input(name="Drop", folder="drop"))
    services.delete_mapping(session, config.media_root, keep, delete_files=False)
    services.delete_mapping(session, config.media_root, drop, delete_files=True)
    assert (config.media_root / "keep").exists()
    assert not (config.media_root / "drop").exists()
    assert session.scalars(select(Mapping)).all() == []


def test_reorder(session, config, make_mapping, add_video):
    mapping = make_mapping()
    a = put_done(session, mapping, add_video, "a", "A")
    b = put_done(session, mapping, add_video, "b", "B")
    services.reorder(session, config.media_root, mapping, [b.id, a.id])
    assert (a.position, b.position) == (2, 1)
    assert (Path(mapping.folder) / "Season 01" / "My Show S01E01 B.mkv").exists()
    with pytest.raises(ServiceError):
        services.reorder(session, config.media_root, mapping, [a.id])


def test_season_break_toggle(session, config, make_mapping, add_video):
    mapping = make_mapping()
    a = put_done(session, mapping, add_video, "a", "A")
    b = put_done(session, mapping, add_video, "b", "B")
    with pytest.raises(ServiceError):
        services.set_season_break(session, config.media_root, mapping, a.id, True)
    services.set_season_break(session, config.media_root, mapping, b.id, True)
    services.set_season_break(session, config.media_root, mapping, b.id, True)  # idempotent
    assert len(mapping.season_breaks) == 1
    assert (Path(mapping.folder) / "Season 02" / "My Show S02E01 B.mkv").exists()
    services.set_season_break(session, config.media_root, mapping, b.id, False)
    assert mapping.season_breaks == []


def test_rename_show_and_music(session, config, make_mapping, add_video):
    show = make_mapping()
    video = put_done(session, show, add_video, "a", "A")
    services.rename_video(session, config.media_root, video, title="  Better  ")
    assert video.custom_title == "Better" and video.user_edited
    assert (Path(show.folder) / "Season 01" / "My Show S01E01 Better.mkv").exists()
    services.rename_video(session, config.media_root, video, title="")
    assert video.custom_title is None

    music = make_mapping("Music", type=MappingType.MUSIC_VIDEO)
    mv = put_done(session, music, add_video, "m", "x")
    services.rename_video(session, config.media_root, mv, artist="Queen", song="Innuendo")
    assert (Path(music.folder) / "Queen - Innuendo.mkv").exists()
    with pytest.raises(ServiceError):
        services.rename_video(session, config.media_root, mv, artist="", song="x")


def test_delete_video_moves_break_ignores_and_renumbers(session, config, make_mapping, add_video):
    mapping = make_mapping(playlist_url="https://youtube.com/playlist?list=1")
    a = put_done(session, mapping, add_video, "a", "A")
    b = put_done(session, mapping, add_video, "b", "B")
    c = put_done(session, mapping, add_video, "c", "C")
    services.set_season_break(session, config.media_root, mapping, b.id, True)
    services.delete_video(session, config.media_root, b, ignore=True)
    assert [v.youtube_id for v in sorted(mapping.videos, key=lambda v: v.position)] == ["a", "c"]
    assert [v.position for v in sorted(mapping.videos, key=lambda v: v.position)] == [1, 2]
    assert [br.video_id for br in mapping.season_breaks] == [c.id]
    assert [i.youtube_id for i in mapping.ignored] == ["b"]
    folder = Path(mapping.folder)
    assert not any("B" in p.name for p in folder.rglob("*.mkv"))
    assert (folder / "Season 02" / "My Show S02E01 C.mkv").exists()
    assert session.get(Video, a.id) is not None


def test_delete_video_without_playlist_never_ignores(session, config, make_mapping, add_video):
    mapping = make_mapping()
    video = add_video(mapping, "a")
    services.delete_video(session, config.media_root, video, ignore=True)
    assert mapping.ignored == []


def test_unignore_and_retry(session, config, make_mapping, add_video):
    mapping = make_mapping()
    mapping.ignored.append(IgnoredVideo(youtube_id="z", title="Z"))
    session.commit()
    services.unignore(session, mapping, "z")
    assert mapping.ignored == []
    video = add_video(mapping, "f", status=VideoStatus.FAILED, attempts=3, error="boom")
    services.retry_video(session, video)
    assert (video.status, video.attempts, video.error, video.next_retry_at) == (VideoStatus.PENDING, 0, None, None)


def test_mapping_stats(session, make_mapping, add_video):
    mapping = make_mapping()
    add_video(mapping, "a")
    add_video(mapping, "b", status=VideoStatus.DONE)
    add_video(mapping, "c", status=VideoStatus.DONE)
    stats = services.mapping_stats(session)[mapping.id]
    assert stats["done"] == 2 and stats["pending"] == 1 and stats["total"] == 3 and stats["failed"] == 0


def test_delete_mapping_files_only_removes_what_the_app_owns(session, config, add_video):
    mapping = services.create_mapping(session, config.media_root, show_input(folder="existing"))
    folder = Path(mapping.folder)
    (folder / "my-own-movie.mkv").write_text("MINE")
    put_done(session, mapping, add_video, "a", "Ep")
    services.reorder(session, config.media_root, mapping, [v.id for v in mapping.videos])  # places a into Season 01
    assert (folder / "Season 01" / "Veritasium S01E01 Ep.mkv").exists()
    services.delete_mapping(session, config.media_root, mapping, delete_files=True)
    assert (folder / "my-own-movie.mkv").read_text() == "MINE"
    assert not (folder / "Season 01").exists()
    assert not (folder / "tvshow.nfo").exists()
