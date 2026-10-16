from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Naive UTC timestamp; everything stored in the DB uses this."""
    return datetime.now(UTC).replace(tzinfo=None)


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC).astimezone()
