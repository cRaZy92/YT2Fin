import pytest

from app import settings
from app.settings import SettingsError


def test_get_all_returns_defaults(session):
    values = settings.get_all(session)
    assert values["max_height"] == 1080
    assert values["min_delay_seconds"] == 30
    assert values["retry_backoff_minutes"] == [10, 60, 360]


def test_set_many_persists_and_coerces_form_strings(session, session_factory):
    settings.set_many(session, {"max_height": "720", "sleep_requests": "2.5", "ytdlp_auto_update": "off",
                                "retry_backoff_minutes": "5, 30"})
    with session_factory() as other:
        values = settings.get_all(other)
    assert values["max_height"] == 720
    assert values["sleep_requests"] == 2.5
    assert values["ytdlp_auto_update"] is False
    assert values["retry_backoff_minutes"] == [5, 30]


@pytest.mark.parametrize("raw", [
    {"max_height": "999"},
    {"container": "avi"},
    {"min_delay_seconds": "100", "max_delay_seconds": "50"},
    {"concurrency": "0"},
    {"max_filename_length": "20"},
    {"min_delay_seconds": "-1"},
    {"max_height": "abc"},
    {"nope": "1"},
])
def test_set_many_rejects_invalid(session, raw):
    with pytest.raises(SettingsError):
        settings.set_many(session, raw)
    assert settings.get_all(session)["max_height"] == 1080


def test_resolve_applies_only_overridable_non_empty(session, make_mapping):
    mapping = make_mapping()
    mapping.overrides = {"max_height": 480, "container": None, "min_delay_seconds": 1}
    session.commit()
    values = settings.resolve(session, mapping)
    assert values["max_height"] == 480
    assert values["container"] == "mkv"
    assert values["min_delay_seconds"] == 30


def test_coerce_overrides_drops_inherit_values():
    out = settings.coerce_overrides({"max_height": "", "container": "mp4", "subtitle_auto": "false"})
    assert out == {"container": "mp4", "subtitle_auto": False}


def test_coerce_overrides_rejects_non_overridable():
    with pytest.raises(SettingsError):
        settings.coerce_overrides({"concurrency": "2"})


def test_max_filename_length_capped_below_255_bytes_minus_companion_tail(session):
    with pytest.raises(SettingsError):
        settings.set_many(session, {"max_filename_length": "240"})
    settings.set_many(session, {"max_filename_length": "230"})
