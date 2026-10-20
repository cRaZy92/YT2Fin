"""Thin, blocking wrapper over the yt-dlp Python API. Call from worker threads only."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.postprocessor import MetadataParserPP

from app.errors import ErrorKind, classify

log = logging.getLogger("yt_dlp")

MEDIA_EXTS = {".mkv", ".mp4", ".webm", ".m4v", ".mov"}
EXTRA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".srt", ".vtt", ".ass"}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_PLACEHOLDER_TITLES = {"[Private video]", "[Deleted video]"}
_CHANNEL_URL = re.compile(
    r"^(?P<base>https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+))"
)
_VCODEC_FILTER = {"h264": "[vcodec^=avc1]", "av1": "[vcodec^=av01]", "vp9": "[vcodec~='^vp0?9']"}


_pot_provider_url: str | None = None


def configure(*, pot_provider_url: str | None) -> None:
    """Process-wide yt-dlp options that come from the environment (set once at startup)."""
    global _pot_provider_url
    _pot_provider_url = pot_provider_url


class DownloadFailed(Exception):
    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class Cancelled(Exception):
    pass


@dataclass
class Entry:
    youtube_id: str
    title: str
    channel: str | None = None


@dataclass
class VideoInfo:
    youtube_id: str
    title: str
    channel: str | None
    description: str | None
    upload_date: str | None
    duration: int | None
    thumbnail_url: str | None
    artist: str | None
    track: str | None

    @classmethod
    def from_info(cls, info: dict[str, Any]) -> VideoInfo:
        artists = info.get("artists")
        return cls(
            youtube_id=info["id"],
            title=info.get("title") or info["id"],
            channel=info.get("channel") or info.get("uploader"),
            description=info.get("description"),
            upload_date=info.get("upload_date"),
            duration=int(info["duration"]) if info.get("duration") else None,
            thumbnail_url=info.get("thumbnail"),
            artist=", ".join(artists) if artists else info.get("artist"),
            track=info.get("track"),
        )


@dataclass
class ShowInfo:
    """Show-level metadata of a playlist or channel, plus the channel's image URLs by artwork kind."""
    description: str | None
    channel: str | None
    images: dict[str, str]


class _Logger:
    """Routes yt-dlp output into Python logging (and so into the live log view)."""

    def debug(self, msg: str) -> None:
        if msg.startswith("[debug] ") or (msg.startswith("[download]") and "%" in msg):
            return
        log.info(msg)

    def info(self, msg: str) -> None:
        log.info(msg)

    def warning(self, msg: str) -> None:
        log.warning(msg)

    def error(self, msg: str) -> None:
        if "does not have a shorts tab" in msg:  # expected for channels without Shorts; handled by the caller
            log.info(msg)
            return
        log.error(msg)


def video_url(youtube_id: str) -> str:
    return f"https://www.youtube.com/watch?v={youtube_id}"


def ytdlp_version() -> str:
    from yt_dlp.version import __version__

    return __version__


def _clean_message(message: str) -> str:
    return _ANSI.sub("", message).removeprefix("ERROR: ").strip()


def build_format(max_height: int, vcodec: str) -> str:
    height = f"[height<={max_height}]"
    chain = []
    if vcodec in _VCODEC_FILTER:
        chain.append(f"bv*{height}{_VCODEC_FILTER[vcodec]}+ba")
    chain += [f"bv*{height}+ba", f"b{height}", "bv*+ba", "b"]
    return "/".join(chain)


def common_opts(settings: dict[str, Any], cookiefile: Path | None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "noprogress": True,
        "logger": _Logger(),
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "socket_timeout": 30,
    }
    if settings["sleep_requests"]:
        opts["sleep_interval_requests"] = settings["sleep_requests"]
    if cookiefile:
        opts["cookiefile"] = str(cookiefile)
    if _pot_provider_url:
        # PO tokens make requests look like YouTube's own player; without them YouTube answers 403
        opts["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [_pot_provider_url]}}
    return opts


def _skip_live(info: dict[str, Any], *, incomplete: bool) -> str | None:
    """yt-dlp match filter: never record a stream that is live right now."""
    return "Stream is live right now" if info.get("is_live") else None


def download_opts(settings: dict[str, Any], staging_dir: Path, cookiefile: Path | None,
                  progress_hook: Callable[[dict], None], pp_hook: Callable[[dict], None]) -> dict[str, Any]:
    container = settings["container"]
    opts = common_opts(settings, cookiefile)
    opts.update({
        "format": build_format(settings["max_height"], settings["vcodec"]),
        "outtmpl": str(staging_dir / "media.%(ext)s"),
        "merge_output_format": container,
        "writethumbnail": True,
        "noplaylist": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
        "match_filter": _skip_live,
    })
    if settings["ratelimit_kbps"]:
        opts["ratelimit"] = settings["ratelimit_kbps"] * 1024
    if settings["sleep_interval"]:
        opts["sleep_interval"] = settings["sleep_interval"]
        opts["max_sleep_interval"] = max(settings["max_sleep_interval"], settings["sleep_interval"])

    pps: list[dict[str, Any]] = [{"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}]
    if settings["subtitles"] != "off":
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = bool(settings["subtitle_auto"])
        opts["subtitleslangs"] = [lang.strip() for lang in settings["subtitle_langs"].split(",") if lang.strip()]
        if settings["subtitles"] == "sidecar":
            pps.append({"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"})
    if settings["sponsorblock"] != "off":
        categories = {c.strip() for c in settings["sponsorblock_categories"].split(",") if c.strip()}
        pps.append({"key": "SponsorBlock", "categories": categories, "when": "after_filter"})
    pps.append({"key": "FFmpegVideoRemuxer", "preferedformat": container})
    if settings["sponsorblock"] != "off":
        pps.append({
            "key": "ModifyChapters",
            "remove_sponsor_segments": categories if settings["sponsorblock"] == "remove" else set(),
            "sponsorblock_chapter_title": "[SponsorBlock]: %(category_names)l",
            "force_keyframes": False,
        })
    # Blank the embedded description: Jellyfin falls back to it whenever the NFO's cleaned <plot> is empty.
    pps.append({"key": "MetadataParser", "when": "pre_process", "actions": [
        (MetadataParserPP.interpretter, "", f"(?P<meta_{field}>)") for field in ("description", "synopsis")]})
    pps.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})
    if settings["subtitles"] == "embed":
        pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})
    opts["postprocessors"] = pps
    return opts


def entries_from_info(info: dict[str, Any]) -> list[Entry]:
    if info.get("_type") in ("playlist", "multi_video") or "entries" in info:
        entries: list[Entry] = []
        for entry in info.get("entries") or []:
            if not entry or not entry.get("id"):
                continue
            if entry.get("ie_key") == "YoutubeTab" or entry.get("_type") == "playlist":
                continue
            title = entry.get("title") or entry["id"]
            if title in _PLACEHOLDER_TITLES:
                continue
            entries.append(Entry(entry["id"], title, entry.get("channel") or entry.get("uploader")))
        return entries
    return [Entry(info["id"], info.get("title") or info["id"], info.get("channel") or info.get("uploader"))]


def fetch_entries(url: str, settings: dict[str, Any], cookiefile: Path | None) -> list[Entry]:
    opts = common_opts(settings, cookiefile) | {"extract_flat": "in_playlist", "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.YoutubeDLError as exc:
        message = _clean_message(str(exc))
        raise DownloadFailed(classify(message), message) from exc
    return entries_from_info(info or {})


def fetch_video_info(youtube_id: str, settings: dict[str, Any], cookiefile: Path | None) -> VideoInfo:
    """Full metadata (incl. description) of one video, without downloading it."""
    opts = common_opts(settings, cookiefile) | {"skip_download": True, "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url(youtube_id), download=False)
    except yt_dlp.utils.YoutubeDLError as exc:
        message = _clean_message(str(exc))
        raise DownloadFailed(classify(message), message) from exc
    return VideoInfo.from_info(info)


def is_channel_url(url: str) -> bool:
    return _CHANNEL_URL.match(url.strip()) is not None


def channel_tab_urls(url: str, include_shorts: bool) -> list[str]:
    """Regular uploads live in the channel's /videos tab, Shorts in /shorts."""
    base = _CHANNEL_URL.match(url.strip()).group("base")
    return [f"{base}/videos"] + ([f"{base}/shorts"] if include_shorts else [])


def fetch_source_entries(url: str, settings: dict[str, Any], cookiefile: Path | None,
                         include_shorts: bool = False) -> list[Entry]:
    """Entries of a playlist (in playlist order) or a channel (oldest upload first)."""
    if not is_channel_url(url):
        return fetch_entries(url, settings, cookiefile)
    entries: list[Entry] = []
    for tab_url in channel_tab_urls(url, include_shorts):
        try:
            tab = fetch_entries(tab_url, settings, cookiefile)
        except DownloadFailed as exc:
            if tab_url.endswith("/shorts") and "does not have a shorts tab" in exc.message.lower():
                continue
            raise
        entries.extend(reversed(tab))  # YouTube lists newest first
    return entries


def channel_images(thumbnails: list[dict[str, Any]] | None) -> dict[str, str]:
    """Avatar as poster, full 16:9 channel art as backdrop (fanart), widest cropped strip as banner."""
    images: dict[str, str] = {}
    strips: list[tuple[int, str]] = []
    for thumb in thumbnails or []:
        url, tid = thumb.get("url"), str(thumb.get("id", ""))
        if not url:
            continue
        if tid == "avatar_uncropped":
            images["poster"] = url
        elif tid == "banner_uncropped":
            images["fanart"] = url
        elif (thumb.get("width") or 0) > 3 * (thumb.get("height") or 10**9):
            strips.append((thumb["width"], url))
    if strips:
        images["banner"] = max(strips)[1]
    return images


def _tab_info(url: str, settings: dict[str, Any], cookiefile: Path | None) -> dict[str, Any]:
    """Page-level metadata of a playlist or channel without listing its videos."""
    opts = common_opts(settings, cookiefile) | {"extract_flat": "in_playlist", "skip_download": True,
                                                "playlist_items": "0"}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except yt_dlp.utils.YoutubeDLError as exc:
        message = _clean_message(str(exc))
        raise DownloadFailed(classify(message), message) from exc


def fetch_show_info(url: str, settings: dict[str, Any], cookiefile: Path | None) -> ShowInfo:
    """Description, channel name and channel images; a playlist also needs its channel's page."""
    if is_channel_url(url):
        page = channel = _tab_info(_CHANNEL_URL.match(url.strip()).group("base"), settings, cookiefile)
    else:
        page = _tab_info(url, settings, cookiefile)
        channel = _tab_info(page["channel_url"], settings, cookiefile) if page.get("channel_url") else {}
    return ShowInfo(
        description=page.get("description") or channel.get("description") or None,
        channel=page.get("channel") or page.get("uploader") or channel.get("channel"),
        images=channel_images(channel.get("thumbnails")),
    )


def collect_outputs(staging_dir: Path) -> tuple[Path | None, list[Path]]:
    files = [p for p in staging_dir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl", ".temp"))]
    media = max((p for p in files if p.suffix.lower() in MEDIA_EXTS), key=lambda p: p.stat().st_size, default=None)
    extras = [p for p in files if p.suffix.lower() in EXTRA_EXTS]
    return media, extras


_STAGE_LABELS = {
    "Merger": "Merging video and audio",
    "ThumbnailsConvertor": "Converting thumbnail",
    "SubtitlesConvertor": "Converting subtitles",
    "EmbedSubtitle": "Embedding subtitles",
    "VideoRemuxer": "Remuxing",
    "VideoConvertor": "Converting video",
    "Metadata": "Writing metadata",
    "SponsorBlock": "Fetching SponsorBlock segments",
    "ModifyChapters": "Applying SponsorBlock",
    "FixupM3u8": "Fixing stream container",
    "FixupM4a": "Fixing audio container",
    "FixupDuplicateMoov": "Fixing video container",
    "MoveFiles": "Finishing",
}


def stage_label(postprocessor: str | None) -> str | None:
    """Human label for a yt-dlp postprocessor name ("FFmpegMetadata" -> "Writing metadata")."""
    if not postprocessor:
        return None
    return _STAGE_LABELS.get(postprocessor.removeprefix("FFmpeg"), postprocessor)


def _progress_event(d: dict[str, Any]) -> dict[str, Any]:
    total = d.get("total_bytes") or d.get("total_bytes_estimate")
    done = d.get("downloaded_bytes") or 0
    return {
        "stage": "downloading",
        "percent": round(done * 100 / total, 1) if total else None,
        "speed": d.get("speed"),
        "eta": d.get("eta"),
        "detail": None,
    }


def download(youtube_id: str, settings: dict[str, Any], staging_dir: Path, cookiefile: Path | None,
             on_progress: Callable[[dict], None], is_cancelled: Callable[[], bool]) -> tuple[VideoInfo, Path, list[Path]]:
    staging_dir.mkdir(parents=True, exist_ok=True)

    def progress_hook(d: dict[str, Any]) -> None:
        if is_cancelled():
            raise Cancelled()
        if d.get("status") == "downloading":
            on_progress(_progress_event(d))

    def pp_hook(d: dict[str, Any]) -> None:
        if is_cancelled():
            raise Cancelled()
        if d.get("status") == "started":
            on_progress({"stage": "processing", "percent": None, "speed": None, "eta": None,
                         "detail": stage_label(d.get("postprocessor"))})

    opts = download_opts(settings, staging_dir, cookiefile, progress_hook, pp_hook)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url(youtube_id), download=True)
    except Exception as exc:
        if is_cancelled() or isinstance(exc, Cancelled):
            raise Cancelled() from exc
        message = _clean_message(str(exc))
        kind = classify(message) if isinstance(exc, yt_dlp.utils.YoutubeDLError) else ErrorKind.TRANSIENT
        raise DownloadFailed(kind, message) from exc
    if is_cancelled():
        raise Cancelled()
    media, extras = collect_outputs(staging_dir)
    if media is None:
        if (info or {}).get("live_status") in ("is_live", "is_upcoming"):
            raise DownloadFailed(ErrorKind.NOT_YET, "Stream is live right now; it will be downloaded after it ends")
        raise DownloadFailed(ErrorKind.TRANSIENT, "Download finished but no media file was produced")
    return VideoInfo.from_info(info), media, extras
