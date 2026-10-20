import asyncio
import os
import shutil
import subprocess

import pytest

from app import embedded
from app.models import VideoStatus

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def make_media(path, description="OUR CHANNELS https://kgs.link/youtubeDE"):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=32x32:d=1", "-f", "lavfi",
         "-i", "anullsrc=d=1", "-shortest", "-metadata", "title=Episode", "-metadata", f"description={description}",
         "-metadata", f"synopsis={description}", str(path)],
        check=True, capture_output=True,
    )
    os.utime(path, (1_600_000_000, 1_600_000_000))
    return path


@needs_ffmpeg
@pytest.mark.parametrize("ext", [".mkv", ".mp4"])
def test_strip_removes_description_and_keeps_streams_title_and_mtime(tmp_path, ext):
    media = make_media(tmp_path / f"S01E01{ext}")
    assert embedded.has_description(media)
    embedded.strip(media)
    assert not embedded.has_description(media)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format_tags=title",
                            "-of", "csv=p=0", str(media)], capture_output=True, text=True, check=True).stdout
    assert "video" in probe and "audio" in probe and "Episode" in probe
    assert media.stat().st_mtime == 1_600_000_000
    assert [p.name for p in tmp_path.iterdir()] == [media.name]


@needs_ffmpeg
def test_file_without_description_is_not_reported(tmp_path):
    assert not embedded.has_description(make_media(tmp_path / "a.mkv", description=""))


def test_stripper_rewrites_only_tagged_done_files_and_reports(session_factory, session, make_mapping, add_video,
                                                               config):
    mapping = make_mapping()
    folder = config.media_root / mapping.name
    for name in ("tagged.mkv", "clean.mkv", "broken.mkv"):
        (folder / name).write_bytes(b"x")
    add_video(mapping, "a", status=VideoStatus.DONE, file_path="tagged.mkv")
    add_video(mapping, "b", status=VideoStatus.DONE, file_path="clean.mkv")
    add_video(mapping, "c", status=VideoStatus.DONE, file_path="broken.mkv")
    add_video(mapping, "d", status=VideoStatus.DONE, file_path="gone.mkv")
    add_video(mapping, "e", status=VideoStatus.PENDING)
    rewritten = []

    def rewrite(path):
        if path.name == "broken.mkv":
            raise subprocess.CalledProcessError(1, "ffmpeg", stderr="Invalid data")
        rewritten.append(path.name)

    stripper = embedded.Stripper(session_factory, config.media_root,
                                 check=lambda path: path.name != "clean.mkv", rewrite=rewrite)
    asyncio.run(stripper.run())
    assert rewritten == ["tagged.mkv"]
    assert (stripper.done, stripper.total, stripper.running) == (3, 3, False)
    assert stripper.last_result == "Removed the embedded description from 1 of 3 files; 1 failed (see Logs)"


def test_settings_button_starts_the_job(client, app):
    calls = []

    class FakeStripper:
        running = False
        last_result = None

        async def run(self):
            calls.append("run")

    app.state.stripper = FakeStripper()
    assert "Remove embedded descriptions" in client.get("/settings").text
    response = client.post("/settings/descriptions/strip", follow_redirects=False)
    assert response.status_code == 303 and "background" in response.headers["location"]
    assert calls == ["run"]


def test_settings_page_shows_progress_while_running(client, app):
    class Running:
        running, done, total, last_result = True, 4, 10, None

    app.state.stripper = Running()
    page = client.get("/settings").text
    assert "4 of 10 files checked" in page and "/settings/descriptions/strip" not in page
