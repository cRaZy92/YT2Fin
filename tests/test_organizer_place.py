from pathlib import Path

from app import organizer
from app.models import Video, VideoStatus


def staged(tmp_path: Path, names: dict[str, str]) -> list[Path]:
    stage = tmp_path / "stage"
    stage.mkdir(exist_ok=True)
    paths = []
    for name, content in names.items():
        p = stage / name
        p.write_text(content)
        paths.append(p)
    return paths


def test_place_moves_media_thumb_and_subs(session, config, make_mapping, add_video, tmp_path):
    mapping = make_mapping()
    video = add_video(mapping, "a", title="Hello", status=VideoStatus.DOWNLOADING)
    media, thumb, subs = staged(tmp_path, {"media.mkv": "M", "media.jpg": "T", "media.en.srt": "S"})
    organizer.place(session, config.media_root, mapping, video, media, [thumb, subs], 180)
    season = Path(mapping.folder) / "Season 01"
    assert (season / "My Show S01E01 Hello.mkv").read_text() == "M"
    assert (season / "My Show S01E01 Hello-thumb.jpg").read_text() == "T"
    assert (season / "My Show S01E01 Hello.en.srt").read_text() == "S"
    assert (season / "My Show S01E01 Hello.nfo").exists()
    assert video.status == VideoStatus.DONE
    assert video.file_path == "Season 01/My Show S01E01 Hello.mkv"


def test_place_replaces_previous_download(session, config, make_mapping, add_video, tmp_path):
    mapping = make_mapping()
    folder = Path(mapping.folder)
    old = folder / "old.mp4"
    old.write_text("OLD")
    (folder / "old-thumb.jpg").write_text("OLDTHUMB")
    video = add_video(mapping, "a", title="Hello", status=VideoStatus.DOWNLOADING, file_path="old.mp4")
    [media] = staged(tmp_path, {"media.mkv": "NEW"})
    organizer.place(session, config.media_root, mapping, video, media, [], 180)
    assert not old.exists() and not (folder / "old-thumb.jpg").exists()
    assert (folder / "Season 01" / "My Show S01E01 Hello.mkv").read_text() == "NEW"


def test_delete_video_files(session, config, make_mapping, add_video, tmp_path):
    mapping = make_mapping()
    video = add_video(mapping, "a", title="Hello", status=VideoStatus.DOWNLOADING)
    media, thumb = staged(tmp_path, {"media.mkv": "M", "media.jpg": "T"})
    organizer.place(session, config.media_root, mapping, video, media, [thumb], 180)
    organizer.delete_video_files(config.media_root, mapping, video)
    season = Path(mapping.folder) / "Season 01"
    assert not season.exists() or list(season.iterdir()) == []


def test_delete_mapping_files_refuses_outside_root(make_mapping, config, tmp_path):
    outside = tmp_path / "outside"
    mapping = make_mapping(folder=str(outside))
    (outside / "tvshow.nfo").write_text("x")
    try:
        organizer.delete_mapping_files(config.media_root, mapping)
    except organizer.PathError:
        pass
    assert (outside / "tvshow.nfo").exists()


def test_reconcile(session, config, make_mapping, add_video):
    mapping = make_mapping()
    downloading = add_video(mapping, "d", status=VideoStatus.DOWNLOADING)
    missing = add_video(mapping, "m", status=VideoStatus.DONE, file_path="gone.mkv")
    leftover = config.staging_dir / "5" / "media.mkv.part"
    leftover.parent.mkdir(parents=True)
    leftover.write_text("x")
    organizer.reconcile(session, config.media_root, config.staging_dir, 180)
    session.expire_all()
    assert session.get(Video, downloading.id).status == VideoStatus.PENDING
    reloaded = session.get(Video, missing.id)
    assert reloaded.status == VideoStatus.PENDING and reloaded.file_path is None
    assert config.staging_dir.exists() and list(config.staging_dir.iterdir()) == []


def test_place_never_overwrites_foreign_file(session, config, make_mapping, add_video, tmp_path):
    from app.models import MappingType

    mapping = make_mapping("Music", type=MappingType.MUSIC_VIDEO)
    foreign = Path(mapping.folder) / "X - Y.mkv"
    foreign.write_text("FOREIGN")
    video = add_video(mapping, "a", artist="X", song="Y", status=VideoStatus.DOWNLOADING)
    [media] = staged(tmp_path, {"media.mkv": "NEW"})
    organizer.place(session, config.media_root, mapping, video, media, [], 180)
    assert foreign.read_text() == "FOREIGN"
    assert video.status == VideoStatus.DONE
    assert (Path(mapping.folder) / video.file_path).read_text() == "NEW"


def test_place_does_not_clobber_sibling_holding_the_target_name(session, config, make_mapping, add_video, tmp_path):
    from app.models import MappingType

    mapping = make_mapping("Music", type=MappingType.MUSIC_VIDEO)
    folder = Path(mapping.folder)
    retried = add_video(mapping, "p", artist="Rick", song="Never Gonna", status=VideoStatus.DOWNLOADING)
    (folder / "Rick - Never Gonna.mkv").write_text("SIBLING")
    add_video(mapping, "d", artist="Rick", song="Never Gonna", status=VideoStatus.DONE,
              file_path="Rick - Never Gonna.mkv")
    [media] = staged(tmp_path, {"media.mkv": "RETRIED"})
    organizer.place(session, config.media_root, mapping, retried, media, [], 180)
    assert (folder / "Rick - Never Gonna.mkv").read_text() == "RETRIED"
    assert (folder / "Rick - Never Gonna (2).mkv").read_text() == "SIBLING"
    assert retried.file_path == "Rick - Never Gonna.mkv"


def test_reconcile_leaves_mapping_alone_when_its_folder_is_missing(session, config, make_mapping, add_video):
    import shutil as _shutil

    mapping = make_mapping()
    video = add_video(mapping, "d", status=VideoStatus.DONE, file_path="Season 01/x.mkv")
    _shutil.rmtree(mapping.folder)  # e.g. the NAS share is not mounted
    organizer.reconcile(session, config.media_root, config.staging_dir, 180)
    session.expire_all()
    reloaded = session.get(Video, video.id)
    assert reloaded.status == VideoStatus.DONE and reloaded.file_path == "Season 01/x.mkv"
    assert not Path(mapping.folder).exists()


def test_apply_does_not_recreate_missing_folder(session, config, make_mapping):
    import shutil as _shutil

    mapping = make_mapping()
    _shutil.rmtree(mapping.folder)
    organizer.apply(session, config.media_root, mapping, 180)
    assert not Path(mapping.folder).exists()


def test_place_fails_when_mapping_folder_missing(session, config, make_mapping, add_video, tmp_path):
    import shutil as _shutil

    import pytest

    mapping = make_mapping()
    video = add_video(mapping, "a", status=VideoStatus.DOWNLOADING)
    _shutil.rmtree(mapping.folder)
    [media] = staged(tmp_path, {"media.mkv": "M"})
    with pytest.raises(FileNotFoundError, match="mounted"):
        organizer.place(session, config.media_root, mapping, video, media, [], 180)
    assert not Path(mapping.folder).exists()


def test_reconcile_still_requeues_interrupted_downloads_when_media_is_not_writable(session, config, make_mapping,
                                                                                  add_video):
    video = add_video(make_mapping(), "d", status=VideoStatus.DOWNLOADING)
    config.media_root.chmod(0o555)
    try:
        organizer.reconcile(session, config.media_root, config.staging_dir, 180)
    finally:
        config.media_root.chmod(0o755)
    session.expire_all()
    assert session.get(Video, video.id).status == VideoStatus.PENDING
