from __future__ import annotations

from enum import StrEnum


class ErrorKind(StrEnum):
    PERMANENT = "permanent"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    FORBIDDEN = "forbidden"  # HTTP 403: one-off for a video, or YouTube soft-blocking the IP when repeated
    NOT_YET = "not_yet"  # premiere or live stream: try again later, no retry used


_RATE_LIMIT = (
    "http error 429", "too many requests", "not a bot", "rate-limited", "rate limited",
    "this content isn't available, try again later",
)
_NOT_YET = ("premieres in", "live event will begin", "is not live yet", "is live right now")
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
    if "http error 403" in text:
        return ErrorKind.FORBIDDEN
    if any(p in text for p in _NOT_YET):
        return ErrorKind.NOT_YET
    if any(p in text for p in _PERMANENT):
        return ErrorKind.PERMANENT
    return ErrorKind.TRANSIENT
