import stat

from app import settings as settings_mod

COOKIES = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tf6=40000000\n"


def test_settings_page_shows_values_and_versions(client):
    page = client.get("/settings").text
    assert 'name="max_height"' in page and '<option value="1080" selected' in page
    assert "yt-dlp" in page and "No cookies uploaded" in page


def test_save_settings(client, session):
    response = client.post("/settings", data={"max_height": "720", "min_delay_seconds": "5",
                                              "max_delay_seconds": "10", "retry_backoff_minutes": "1, 2"},
                           follow_redirects=False)
    assert response.status_code == 303 and "msg=" in response.headers["location"]
    session.expire_all()
    values = settings_mod.get_all(session)
    assert values["max_height"] == 720 and values["retry_backoff_minutes"] == [1, 2]
    assert values["ytdlp_auto_update"] is False  # unchecked checkbox = off


def test_save_settings_validation_error(client, session):
    response = client.post("/settings", data={"min_delay_seconds": "100", "max_delay_seconds": "10"})
    assert response.status_code == 400 and "Minimum delay" in response.text
    assert 'value="100"' in response.text
    assert settings_mod.get_all(session)["min_delay_seconds"] == 30


def test_cookies_upload_and_delete(client, config):
    response = client.post("/settings/cookies", files={"file": ("cookies.txt", COOKIES, "text/plain")},
                           follow_redirects=False)
    assert response.status_code == 303
    assert config.cookies_path.read_text() == COOKIES
    assert stat.S_IMODE(config.cookies_path.stat().st_mode) == 0o600
    assert "Cookies uploaded" in client.get("/settings").text
    bad = client.post("/settings/cookies", files={"file": ("x.txt", "hello", "text/plain")})
    assert bad.status_code == 400 and "Netscape" in bad.text
    client.post("/settings/cookies/delete")
    assert not config.cookies_path.exists()


def test_ytdlp_update_button(client, app):
    class FakeUpdater:
        last_result = None
        last_checked_at = None

        async def check(self):
            return "Up to date (2026.01.01)"

    app.state.updater = FakeUpdater()
    response = client.post("/settings/ytdlp/update", follow_redirects=False)
    assert response.status_code == 303 and "Up+to+date" in response.headers["location"]


def test_cookies_file_is_created_private_from_the_start(client, config, monkeypatch):
    import os
    from pathlib import Path

    monkeypatch.setattr(Path, "chmod", lambda self, mode: None)  # no fixing permissions afterwards
    old_umask = os.umask(0)
    try:
        client.post("/settings/cookies", files={"file": ("cookies.txt", COOKIES, "text/plain")})
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(config.cookies_path.stat().st_mode) == 0o600
