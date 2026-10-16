from __future__ import annotations

from enum import StrEnum


class ErrorKind(StrEnum):
    PERMANENT = "permanent"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"


_RATE_LIMIT = (
    "http error 429", "too many requests", "not a bot", "rate-limited", "rate limited",
    "this content isn't available, try again later",
)
_TRANSIENT_OVERRIDES = ("premieres in", "live event will begin", "is not live yet")
_PERMANENT = (
    "private video", "video unavailable", "has been removed", "no longer available",
    "not made this video available in your country", "blocked it in your country",
    "not available in your country", "copyright", "members-only", "join this channel",
    "premium members", "account associated with this video has been terminated",
    "confirm your age", "inappropriate for some users", "does not exist",
)


def classify(message: str) -> ErrorKind:
    text = message.lower().replace("’", "'")
    if any(p in text for p in _RATE_LIMIT):
        return ErrorKind.RATE_LIMIT
    if any(p in text for p in _TRANSIENT_OVERRIDES):
        return ErrorKind.TRANSIENT
    if any(p in text for p in _PERMANENT):
        return ErrorKind.PERMANENT
    return ErrorKind.TRANSIENT
