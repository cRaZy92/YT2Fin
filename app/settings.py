from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Mapping, Setting

DEFAULTS: dict[str, Any] = {
    # quality / format (overridable per mapping)
    "max_height": 1080,
    "container": "mkv",
    "vcodec": "any",
    "subtitles": "off",
    "subtitle_langs": "en",
    "subtitle_auto": False,
    "sponsorblock": "off",
    "sponsorblock_categories": "sponsor,selfpromo,interaction",
    # sync
    "sync_interval_minutes": 360,
    "sync_jitter_seconds": 300,
    # throttling
    "min_delay_seconds": 30,
    "max_delay_seconds": 90,
    "sleep_requests": 1.0,
    "sleep_interval": 0,
    "max_sleep_interval": 0,
    "ratelimit_kbps": 0,
    "concurrency": 1,
    "cooldown_minutes": 60,
    "cooldown_max_minutes": 720,
    # retries
    "retry_backoff_minutes": [10, 60, 360],
    # yt-dlp
    "ytdlp_auto_update": True,
    # naming
    "max_filename_length": 180,
}

CHOICES: dict[str, tuple] = {
    "max_height": (2160, 1440, 1080, 720, 480),
    "container": ("mkv", "mp4"),
    "vcodec": ("any", "h264", "av1", "vp9"),
    "subtitles": ("off", "embed", "sidecar"),
    "sponsorblock": ("off", "mark", "remove"),
}

OVERRIDABLE = frozenset({
    "max_height", "container", "vcodec", "subtitles", "subtitle_langs",
    "subtitle_auto", "sponsorblock", "sponsorblock_categories",
})

BOOL_KEYS = frozenset(k for k, v in DEFAULTS.items() if isinstance(v, bool))


class SettingsError(ValueError):
    pass


def coerce(key: str, raw: Any) -> Any:
    if key not in DEFAULTS:
        raise SettingsError(f"Unknown setting: {key}")
    default = DEFAULTS[key]
    try:
        if isinstance(default, bool):
            value: Any = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "on", "yes")
        elif isinstance(default, int):
            value = int(raw)
        elif isinstance(default, float):
            value = float(raw)
        elif isinstance(default, list):
            parts = raw if isinstance(raw, list) else [p for p in str(raw).replace(" ", "").split(",") if p]
            value = [int(p) for p in parts]
        else:
            value = str(raw).strip()
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"Invalid value for {key}: {raw!r}") from exc
    if key in CHOICES and value not in CHOICES[key]:
        raise SettingsError(f"{key} must be one of {', '.join(map(str, CHOICES[key]))}")
    numbers = value if isinstance(value, list) else [value]
    if any(isinstance(n, (int, float)) and not isinstance(n, bool) and n < 0 for n in numbers):
        raise SettingsError(f"{key} must not be negative")
    return value


def _validate(values: dict[str, Any]) -> None:
    if values["min_delay_seconds"] > values["max_delay_seconds"]:
        raise SettingsError("Minimum delay between downloads must not exceed the maximum delay")
    if values["concurrency"] < 1:
        raise SettingsError("Concurrent downloads must be at least 1")
    if not 60 <= values["max_filename_length"] <= 240:
        raise SettingsError("Max filename length must be between 60 and 240")
    if values["max_sleep_interval"] and values["max_sleep_interval"] < values["sleep_interval"]:
        raise SettingsError("Max sleep interval must be at least the sleep interval")
    if values["cooldown_minutes"] < 1 or values["cooldown_max_minutes"] < values["cooldown_minutes"]:
        raise SettingsError("Cool-down must be at least 1 minute and not exceed the maximum cool-down")
    if values["sync_interval_minutes"] < 5:
        raise SettingsError("Sync interval must be at least 5 minutes")


def get_all(session: Session) -> dict[str, Any]:
    values = copy.deepcopy(DEFAULTS)
    for row in session.scalars(select(Setting)):
        if row.key in values:
            values[row.key] = row.value
    return values


def set_many(session: Session, raw: dict[str, Any]) -> dict[str, Any]:
    new = {key: coerce(key, value) for key, value in raw.items()}
    merged = {**get_all(session), **new}
    _validate(merged)
    for key, value in new.items():
        session.merge(Setting(key=key, value=value))
    session.commit()
    return merged


def resolve(session: Session, mapping: Mapping) -> dict[str, Any]:
    values = get_all(session)
    for key, value in (mapping.overrides or {}).items():
        if key in OVERRIDABLE and value is not None:
            values[key] = value
    return values


def coerce_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in OVERRIDABLE:
            raise SettingsError(f"{key} cannot be overridden per mapping")
        if value is None or value == "":
            continue
        out[key] = coerce(key, value)
    return out
