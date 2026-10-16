"""Pure naming rules. No DB, no filesystem."""
from __future__ import annotations

import re
from dataclasses import dataclass

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_SPACES = re.compile(r"\s+")


def _nbytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _clip(text: str, max_bytes: int) -> str:
    """Cut to at most max_bytes of UTF-8 without splitting a character (Linux names are byte-limited)."""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    return data[:max(max_bytes, 0)].decode("utf-8", errors="ignore")


def sanitize(name: str, max_len: int = 180, fallback: str = "untitled") -> str:
    """max_len is in UTF-8 bytes."""
    cleaned = _INVALID.sub(" ", name)
    cleaned = _SPACES.sub(" ", cleaned).strip().lstrip(".").strip()
    cleaned = _clip(cleaned, max_len).rstrip(" .")
    return cleaned or fallback


@dataclass(frozen=True)
class NamingItem:
    video_id: int
    title: str = ""
    artist: str = ""
    song: str = ""
    season_break: bool = False


def season_episode(items: list[NamingItem]) -> dict[int, tuple[int, int]]:
    result: dict[int, tuple[int, int]] = {}
    season, episode = 1, 0
    for index, item in enumerate(items):
        if item.season_break and index > 0:
            season += 1
            episode = 0
        episode += 1
        result[item.video_id] = (season, episode)
    return result


def episode_code(season: int, episode: int) -> str:
    return f"S{season:02d}E{episode:02d}"


def _show_stem(show_title: str, season: int, episode: int, title: str, max_len: int) -> str:
    prefix = f"{sanitize(show_title, max_len // 2, fallback='Show')} {episode_code(season, episode)}"
    title_part = sanitize(title, max(max_len - _nbytes(prefix) - 1, 0), fallback="")
    name = f"{prefix} {title_part}" if title_part else prefix
    return f"Season {season:02d}/{name}"


def _music_stem(artist: str, song: str, max_len: int) -> str:
    artist_part = sanitize(artist, max_len // 2, fallback="Unknown Artist")
    song_part = sanitize(song, max_len - _nbytes(artist_part) - 3, fallback="Untitled")
    return f"{artist_part} - {song_part}"


def compute_stems(mapping_type: str, show_title: str, items: list[NamingItem], max_len: int = 180) -> dict[int, str]:
    if mapping_type == "show":
        numbers = season_episode(items)
        return {i.video_id: _show_stem(show_title, *numbers[i.video_id], i.title, max_len) for i in items}
    if mapping_type == "music_video":
        stems: dict[int, str] = {}
        used: set[str] = set()
        for item in items:
            base = _music_stem(item.artist, item.song, max_len - 5)
            candidate, n = base, 1
            while candidate.casefold() in used:
                n += 1
                candidate = f"{base} ({n})"
            used.add(candidate.casefold())
            stems[item.video_id] = candidate
        return stems
    raise ValueError(f"Unknown mapping type: {mapping_type}")
