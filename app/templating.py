from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.timeutil import to_local

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def localtime(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    local = to_local(dt)
    return local.strftime(fmt) if local else "never"


def isoutc(dt: datetime | None) -> str:
    return dt.replace(tzinfo=UTC).isoformat() if dt else ""


def speed(bytes_per_second: float | None) -> str:
    if not bytes_per_second:
        return ""
    value = float(bytes_per_second)
    for unit in ("B/s", "KiB/s", "MiB/s"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB/s"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def filesize(num_bytes: float | None) -> str:
    value = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def runtime(seconds: float | None) -> str:
    """Long durations for totals: "41 h 20 m", "5 m"."""
    minutes = int(seconds or 0) // 60
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} m" if hours else f"{minutes} m"


templates.env.filters.update(localtime=localtime, isoutc=isoutc, speed=speed, duration=duration,
                             filesize=filesize, runtime=runtime)
