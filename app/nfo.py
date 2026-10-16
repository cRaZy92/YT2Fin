"""Kodi-style NFO files, which Jellyfin's NFO metadata reader understands."""
from __future__ import annotations

import re
from xml.etree import ElementTree as ET

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_HEADER = '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'


def format_date(yyyymmdd: str | None) -> str | None:
    if not yyyymmdd or len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
        return None
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def _add(parent: ET.Element, tag: str, value: object) -> None:
    if value is None or value == "":
        return
    ET.SubElement(parent, tag).text = _CONTROL.sub("", str(value))


def _uniqueid(parent: ET.Element, youtube_id: str) -> None:
    uid = ET.SubElement(parent, "uniqueid", type="youtube", default="true")
    uid.text = youtube_id


def _render(root: ET.Element) -> str:
    ET.indent(root)
    return _HEADER + ET.tostring(root, encoding="unicode") + "\n"


def tvshow_nfo(title: str) -> str:
    root = ET.Element("tvshow")
    _add(root, "title", title)
    return _render(root)


def episode_nfo(*, title: str, show_title: str, season: int, episode: int, plot: str | None,
                aired: str | None, youtube_id: str) -> str:
    root = ET.Element("episodedetails")
    _add(root, "title", title)
    _add(root, "showtitle", show_title)
    _add(root, "season", season)
    _add(root, "episode", episode)
    _add(root, "plot", plot)
    _add(root, "aired", format_date(aired))
    _uniqueid(root, youtube_id)
    return _render(root)


def musicvideo_nfo(*, title: str, artist: str, plot: str | None, premiered: str | None, youtube_id: str) -> str:
    root = ET.Element("musicvideo")
    date = format_date(premiered)
    _add(root, "title", title)
    _add(root, "artist", artist)
    _add(root, "plot", plot)
    _add(root, "premiered", date)
    _add(root, "year", date[:4] if date else None)
    _uniqueid(root, youtube_id)
    return _render(root)
