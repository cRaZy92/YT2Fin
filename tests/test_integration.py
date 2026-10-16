import pytest

from app import settings as settings_mod
from app import ytdlp_client


@pytest.mark.integration
def test_real_download_of_short_video(tmp_path):
    values = dict(settings_mod.DEFAULTS, max_height=480, sleep_requests=0)
    events = []
    info, media, extras = ytdlp_client.download("jNQXAC9IVRw", values, tmp_path / "stage", None,
                                                events.append, lambda: False)
    assert info.youtube_id == "jNQXAC9IVRw" and info.title
    assert media.exists() and media.suffix == ".mkv" and media.stat().st_size > 10_000
    assert any(p.suffix == ".jpg" for p in extras)
    assert events
