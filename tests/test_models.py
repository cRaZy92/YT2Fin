import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import IgnoredVideo, Mapping, SeasonBreak, Video, VideoStatus


def test_videos_ordered_by_position(session, make_mapping, add_video):
    mapping = make_mapping()
    add_video(mapping, "b", position=2)
    add_video(mapping, "a", position=1)
    session.expire_all()
    assert [v.youtube_id for v in session.get(Mapping, mapping.id).videos] == ["a", "b"]


def test_youtube_id_unique_per_mapping(session, make_mapping, add_video):
    mapping = make_mapping()
    add_video(mapping, "dup")
    with pytest.raises(IntegrityError):
        add_video(mapping, "dup")
    session.rollback()


def test_same_youtube_id_allowed_in_two_mappings(make_mapping, add_video):
    add_video(make_mapping("One"), "same")
    add_video(make_mapping("Two"), "same")


def test_deleting_mapping_cascades(session, make_mapping, add_video):
    mapping = make_mapping()
    video = add_video(mapping, "x")
    mapping.season_breaks.append(SeasonBreak(video_id=video.id))
    mapping.ignored.append(IgnoredVideo(youtube_id="y", title="Y"))
    session.commit()
    session.delete(mapping)
    session.commit()
    for model in (Video, SeasonBreak, IgnoredVideo):
        assert session.scalar(select(func.count()).select_from(model)) == 0


def test_video_defaults_and_display_title(make_mapping, add_video):
    video = add_video(make_mapping(), "abc", title="Hello")
    assert video.status == VideoStatus.PENDING
    assert video.attempts == 0
    assert video.user_edited is False
    assert video.display_title == "Hello"
    video.custom_title = "Custom"
    assert video.display_title == "Custom"
