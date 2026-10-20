from pathlib import Path

import pytest

from app import organizer
from app.models import MappingType, SeasonBreak, VideoStatus
from app.organizer import PathError


def put(folder: Path, rel: str, content: str) -> Path:
    path = folder / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.fixture
def done_video(add_video):
    def _done(mapping, yid, rel, title=None, **kw):
        folder = Path(mapping.folder)
        put(folder, rel, f"media {yid}")
        put(folder, str(Path(rel).with_suffix("")) + "-thumb.jpg", f"thumb {yid}")
        return add_video(mapping, yid, title=title, status=VideoStatus.DONE, file_path=rel, **kw)
    return _done


def apply(session, config, mapping):
    organizer.apply(session, config.media_root, mapping, 180)


def test_ensure_under_root(config):
    root = config.media_root
    assert organizer.ensure_under_root(root, root / "a" / "b") == (root / "a" / "b").resolve()
    for bad in [root, root / ".." / "x", Path("/etc"), root / ".ytjs-tmp" / "x"]:
        with pytest.raises(PathError):
            organizer.ensure_under_root(root, bad)


def test_companion_files(tmp_path):
    media = put(tmp_path, "Show S01E01 A.mkv", "m")
    expected = {put(tmp_path, n, "c") for n in ["Show S01E01 A.nfo", "Show S01E01 A-thumb.jpg", "Show S01E01 A.en.srt"]}
    put(tmp_path, "Show S01E01 A B.mkv", "other")
    put(tmp_path, "Show S01E01 A B.nfo", "other")
    assert set(organizer.companion_files(media)) == expected


def test_apply_places_files_with_companions_and_nfo(session, config, make_mapping, done_video):
    mapping = make_mapping()
    done_video(mapping, "a", "random/a.mkv", title="First")
    apply(session, config, mapping)
    folder = Path(mapping.folder)
    media = folder / "Season 01" / "My Show S01E01 First.mkv"
    assert media.read_text() == "media a"
    assert (folder / "Season 01" / "My Show S01E01 First-thumb.jpg").read_text() == "thumb a"
    nfo = (folder / "Season 01" / "My Show S01E01 First.nfo").read_text()
    assert "<episode>1</episode>" in nfo and "<title>First</title>" in nfo
    assert "<title>My Show</title>" in (folder / "tvshow.nfo").read_text()
    assert mapping.videos[0].file_path == "Season 01/My Show S01E01 First.mkv"


def test_apply_swaps_two_episodes_without_collision(session, config, make_mapping, done_video):
    mapping = make_mapping()
    a = done_video(mapping, "a", "x/a.mkv", title="A")
    b = done_video(mapping, "b", "x/b.mkv", title="B")
    apply(session, config, mapping)
    a.position, b.position = 2, 1
    session.commit()
    apply(session, config, mapping)
    season = Path(mapping.folder) / "Season 01"
    assert (season / "My Show S01E01 B.mkv").read_text() == "media b"
    assert (season / "My Show S01E02 A.mkv").read_text() == "media a"
    assert (season / "My Show S01E02 A-thumb.jpg").read_text() == "thumb a"
    assert "<episode>2</episode>" in (season / "My Show S01E02 A.nfo").read_text()
    assert sorted(p.name for p in season.iterdir()) == sorted([
        "My Show S01E01 B.mkv", "My Show S01E01 B-thumb.jpg", "My Show S01E01 B.nfo",
        "My Show S01E02 A.mkv", "My Show S01E02 A-thumb.jpg", "My Show S01E02 A.nfo",
    ])


def test_season_break_moves_files_and_cleans_empty_dirs(session, config, make_mapping, done_video):
    mapping = make_mapping()
    done_video(mapping, "a", "x/a.mkv", title="A")
    b = done_video(mapping, "b", "x/b.mkv", title="B")
    apply(session, config, mapping)
    mapping.season_breaks.append(SeasonBreak(video_id=b.id))
    session.commit()
    apply(session, config, mapping)
    folder = Path(mapping.folder)
    assert (folder / "Season 02" / "My Show S02E01 B.mkv").exists()
    mapping.season_breaks.clear()
    session.commit()
    apply(session, config, mapping)
    assert (folder / "Season 01" / "My Show S01E02 B.mkv").exists()
    assert not (folder / "Season 02").exists()
    assert not (folder / "x").exists() or not any((folder / "x").iterdir())


def test_unavailable_videos_do_not_consume_numbers_and_pass_on_breaks(session, config, make_mapping, add_video, done_video):
    mapping = make_mapping()
    done_video(mapping, "a", "x/a.mkv", title="A")
    gone = add_video(mapping, "gone", status=VideoStatus.UNAVAILABLE)
    done_video(mapping, "c", "x/c.mkv", title="C")
    mapping.season_breaks.append(SeasonBreak(video_id=gone.id))
    session.commit()
    apply(session, config, mapping)
    assert (Path(mapping.folder) / "Season 02" / "My Show S02E01 C.mkv").exists()


def test_pending_videos_reserve_episode_numbers(session, config, make_mapping, add_video, done_video):
    mapping = make_mapping()
    add_video(mapping, "p")
    done_video(mapping, "d", "x/d.mkv", title="D")
    apply(session, config, mapping)
    assert (Path(mapping.folder) / "Season 01" / "My Show S01E02 D.mkv").exists()


def test_custom_title_renames(session, config, make_mapping, done_video):
    mapping = make_mapping()
    video = done_video(mapping, "a", "x/a.mkv", title="Old")
    apply(session, config, mapping)
    video.custom_title = "New"
    session.commit()
    apply(session, config, mapping)
    assert (Path(mapping.folder) / "Season 01" / "My Show S01E01 New.mkv").read_text() == "media a"
    assert "<title>New</title>" in (Path(mapping.folder) / "Season 01" / "My Show S01E01 New.nfo").read_text()


def test_music_video_layout(session, config, make_mapping, done_video):
    mapping = make_mapping("Music", type=MappingType.MUSIC_VIDEO)
    done_video(mapping, "a", "a.mkv", artist="Queen", song="Bohemian Rhapsody")
    done_video(mapping, "b", "b.mkv", artist="Queen", song="Bohemian Rhapsody")
    apply(session, config, mapping)
    folder = Path(mapping.folder)
    assert (folder / "Queen - Bohemian Rhapsody.mkv").read_text() == "media a"
    assert (folder / "Queen - Bohemian Rhapsody (2).mkv").read_text() == "media b"
    nfo = (folder / "Queen - Bohemian Rhapsody.nfo").read_text()
    assert "<artist>Queen</artist>" in nfo and "<title>Bohemian Rhapsody</title>" in nfo
    assert not (folder / "tvshow.nfo").exists()


def test_apply_never_overwrites_foreign_file(session, config, make_mapping, done_video):
    mapping = make_mapping()
    a = done_video(mapping, "a", "x/a.mkv", title="A")
    b = done_video(mapping, "b", "x/b.mkv", title="B")
    folder = Path(mapping.folder)
    foreign = put(folder, "Season 01/My Show S01E01 A.mkv", "FOREIGN")
    apply(session, config, mapping)
    assert foreign.read_text() == "FOREIGN"
    assert (folder / "x" / "a.mkv").read_text() == "media a"
    assert a.file_path == "x/a.mkv"
    assert (folder / "Season 01" / "My Show S01E02 B.mkv").read_text() == "media b"
    assert b.file_path == "Season 01/My Show S01E02 B.mkv"


def test_apply_is_idempotent(session, config, make_mapping, done_video):
    mapping = make_mapping()
    done_video(mapping, "a", "x/a.mkv", title="A")
    apply(session, config, mapping)
    nfo = Path(mapping.folder) / "Season 01" / "My Show S01E01 A.nfo"
    mtime = nfo.stat().st_mtime_ns
    apply(session, config, mapping)
    assert nfo.stat().st_mtime_ns == mtime


def test_poster_copied_from_first_episode_thumb(session, config, make_mapping, done_video):
    mapping = make_mapping()
    done_video(mapping, "a", "x/a.mkv", title="A")
    apply(session, config, mapping)
    assert (Path(mapping.folder) / "poster.jpg").read_text() == "thumb a"


def test_execute_moves_rolls_back_when_a_move_fails(tmp_path):
    a = put(tmp_path, "a.mkv", "A")
    b = put(tmp_path, "b.mkv", "B")
    blocker = put(tmp_path, "y.mkv", "BLOCK")
    with pytest.raises(FileExistsError):
        organizer.execute_moves([(a, tmp_path / "x.mkv"), (b, blocker)])
    assert a.read_text() == "A" and b.read_text() == "B" and blocker.read_text() == "BLOCK"
    assert not (tmp_path / "x.mkv").exists()
    assert not list(tmp_path.glob(".ytjs-move-*"))


def test_existing_foreign_tvshow_nfo_is_kept_but_ours_is_updated(session, config, make_mapping):
    mapping = make_mapping()
    folder = Path(mapping.folder)
    (folder / "tvshow.nfo").write_text("<tvshow><title>Curated</title><tvdbid>1</tvdbid></tvshow>")
    apply(session, config, mapping)
    assert "Curated" in (folder / "tvshow.nfo").read_text()

    other = make_mapping("Other")
    apply(session, config, other)
    other.show_title = "Renamed"
    session.commit()
    apply(session, config, other)
    assert "<title>Renamed</title>" in (Path(other.folder) / "tvshow.nfo").read_text()


def test_apply_only_removes_empty_season_folders(session, config, make_mapping, done_video):
    mapping = make_mapping()
    folder = Path(mapping.folder)
    (folder / "extras").mkdir()
    (folder / "Season 07").mkdir()
    done_video(mapping, "a", "x/a.mkv", title="A")
    apply(session, config, mapping)
    assert (folder / "extras").is_dir()
    assert not (folder / "Season 07").exists()


def test_tvshow_nfo_written_by_older_versions_is_still_updated(session, config, make_mapping):
    mapping = make_mapping()
    folder = Path(mapping.folder)
    (folder / "tvshow.nfo").write_text(
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<tvshow>\n  <title>My Show</title>\n</tvshow>\n')
    mapping.show_title = "New Name"
    session.commit()
    apply(session, config, mapping)
    assert "<title>New Name</title>" in (folder / "tvshow.nfo").read_text()


DESC = "The actual story of this video.\n\nThis video is sponsored by Acme! Use code X: https://acme.example\nAcmeCorp rocks."


@pytest.mark.parametrize("mode, extra, expected_plot", [
    ("cleaned", None, "The actual story of this video.\n\nAcmeCorp rocks."),
    ("cleaned", "acmecorp", "The actual story of this video."),
    ("first_paragraph", None, "The actual story of this video."),
    ("full", None, DESC),
    ("none", None, None),
])
def test_nfo_plot_follows_the_mappings_description_mode(session, config, make_mapping, done_video, mode, extra,
                                                       expected_plot):
    from xml.etree import ElementTree as ET

    mapping = make_mapping(description_mode=mode, description_remove=extra)
    done_video(mapping, "a", "x/a.mkv", title="A", description=DESC)
    apply(session, config, mapping)
    nfo = (Path(mapping.folder) / "Season 01" / "My Show S01E01 A.nfo").read_text()
    assert ET.fromstring(nfo.split("\n", 1)[1]).findtext("plot") == expected_plot


def test_disk_space_reports_free_and_total(tmp_path):
    free, total = organizer.disk_space(tmp_path)
    assert 0 < free <= total


def test_disk_space_is_none_for_a_missing_folder(tmp_path):
    assert organizer.disk_space(tmp_path / "missing") is None


def test_tvshow_nfo_gets_channel_description_studio_and_first_air_date(session, config, make_mapping, done_video):
    mapping = make_mapping(source_description="About.\n\nFollow me: https://x.example", description_mode="first_paragraph")
    done_video(mapping, "a", "x/a.mkv", title="A", upload_date="20210102", channel="Chan")
    done_video(mapping, "b", "x/b.mkv", title="B", upload_date="20200101", channel="Chan", duration=600)
    apply(session, config, mapping)
    folder = Path(mapping.folder)
    show = (folder / "tvshow.nfo").read_text()
    assert "<plot>About.</plot>" in show and "<studio>Chan</studio>" in show and "<premiered>2020-01-01" in show
    episode = (folder / "Season 01" / "My Show S01E02 B.nfo").read_text()
    assert "<runtime>10</runtime>" in episode and "<studio>Chan</studio>" in episode


def test_episode_poster_is_recorded_and_removed_with_the_mapping(session, config, make_mapping, done_video):
    mapping = make_mapping()
    done_video(mapping, "a", "x/a.mkv", title="A")
    apply(session, config, mapping)
    session.expire_all()
    assert mapping.artwork["poster"]["source"] == "episode"
    organizer.delete_mapping_files(config.media_root, mapping)
    assert not (Path(mapping.folder) / "poster.jpg").exists()
