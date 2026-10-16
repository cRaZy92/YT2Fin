"""Best-effort artist/song extraction from messy YouTube titles."""
from __future__ import annotations

import re

_NOISE_WORDS = (
    r"official|video|audio|lyrics?|visuali[sz]er|hd|hq|4k|8k|1080p|720p|"
    r"remaster(?:ed)?|mv|m/v|clip officiel|videoclip|explicit|color coded"
)
_BRACKETED_NOISE = re.compile(
    r"\s*[\(\[【][^\)\]】]*?\b(?:" + _NOISE_WORDS + r")\b[^\)\]】]*?[\)\]】]",
    re.IGNORECASE,
)
_SEPARATOR = re.compile(r"\s+[-–—]\s+")
_CHANNEL_SUFFIX = re.compile(
    r"(?:\s*-\s*topic|vevo|\s+official(?:\s+(?:artist\s+)?channel)?)\s*$", re.IGNORECASE
)
_QUOTES = "\"'“”‘’"


def strip_noise(text: str) -> str:
    cleaned = _BRACKETED_NOISE.sub("", text)
    if " | " in cleaned:
        cleaned = cleaned.split(" | ", 1)[0]
    return cleaned.strip().strip(_QUOTES).strip()


def clean_channel(channel: str | None) -> str:
    if not channel:
        return ""
    name, previous = channel.strip(), None
    while previous != name:
        previous = name
        name = _CHANNEL_SUFFIX.sub("", name).strip()
    return name


def parse_artist_song(
    title: str,
    channel: str | None = None,
    yt_artist: str | None = None,
    yt_track: str | None = None,
) -> tuple[str, str]:
    if yt_artist and yt_track:
        return yt_artist.strip(), strip_noise(yt_track)
    parts = _SEPARATOR.split(title, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return strip_noise(parts[0]), strip_noise(parts[1])
    return clean_channel(channel) or "Unknown Artist", strip_noise(title)
