"""Strip promos, links and footers from YouTube descriptions before they go into NFO files. Pure."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

MODES = ("cleaned", "first_paragraph", "full", "none")

# A line that starts the boilerplate footer: everything from here on is dropped.
_FOOTER_START = re.compile(
    r"^\W*(?:[-=_▬━─═*~•·]{3,}"
    r"|follow (?:me|us)\b|socials?\b|social media\b|connect with\b|find (?:me|us)\b"
    r"|business (?:inquir|enquir|email|contact)|for business\b"
    r"|music (?:by|used|from)\b|music:|credits\b|my gear\b|gear i use\b"
    r"|(?:check out )?our (?:channel )?partners\b|channel partners\b|purchases made\b|disclosure\b"
    r"|sources?\b[^:]{0,40}:|references\s*:|further reading\b|copyright\b|©|acknowledge?ments?\W*$"
    # Channel-section headings ("OUR CHANNELS", "HOW CAN YOU SUPPORT US?", "OUR MUSIC ♬♪"): alone on their line
    r"|our (?:other )?(?:channels?|voice|music|team|links)\W*$|how (?:can|to) (?:you )?support (?:us|me|the channel)\b"
    r"|discussions?\b.*\bsocial media\b|(?:the )?soundtrack (?:of|for|from) this video\b|additional music\b)"
    # Production credits: "Hosted by: …", "Sound Editor: …", "All music by: …" (a label, then a colon)
    r"|^\W*(?:[\w-]+\s+){0,2}(?:(?:host|written|direct|produc|edit|narrat|animat|present|research|film|scor|mix"
    r"|illustrat|creat|compos)\w*\s+by|(?:host|writer|director|producer|editor|narrator|animator|composer|researcher"
    r"|cinematographer|camera|design|presenter|illustrator|colou?ri[sz]\w*|music|sound|script|voice|costume|actor"
    r"|actress|extra|operator|assist(?:ant)?)s?(?:\s+by)?)\s*:",
    re.IGNORECASE,
)
# Phrases that mark the start of disclosures wherever they appear in the line.
_FOOTER_ANYWHERE = re.compile(
    r"\b(?:is an investor in|we may earn (?:a )?commission|as an amazon associate|all rights reserved"
    r"|co-production|subtitle files)\b",
    re.IGNORECASE,
)
# Single lines that are promotional or navigational rather than about the video.
_DROP_LINE = re.compile(
    r"https?://|www\.|\b[\w-]+\.(?:com|net|org|io|gg|ly|ee|store|shop|tv|me|co|link)\b/?"
    r"|[\w.+-]+@[\w-]+\.\w+"                                    # e-mail
    r"|(?:^|\s)@\w+"                                            # social handle
    r"|^\s*[(\[]?\d{1,2}:\d{2}(?::\d{2})?[)\]]?\s"              # chapter timestamp
    r"|\b(?:sponsor(?:ed|s)?|affiliate|patreon|merch|subscribe|membership|linktree)\b"
    r"|\b(?:use|promo|discount) code\b|\d+\s?% off\b|\bfree trial\b|\bjoin this channel\b|\blink in\b"
    r"|^\W*(?:timestamps|chapters)\b"
    r"|^\W*support (?:me|us|the channel|this channel|the show|my work|our work)\b"
    r"|\bmeans of support\b|\bhelps? us (?:to )?(?:continue|keep (?:going|making|doing))\b"
    r"|^\W*check out\b.*\b(?:channels?|links?|socials?|website|podcast)\b"
    r"|\b(?:instagram|twitter|tiktok|discord|facebook|twitch|snapchat|threads|bluesky|reddit|mastodon)\b\s*[:|\-–]"
    r"|\b(?:supplied by|courtesy of|licensed (?:from|by|under)|stock footage)\b"
    r"|\bhuge thanks to\b.*\bfor letting us\b",
    re.IGNORECASE,
)
_HASHTAG = re.compile(r"#\w+")


def _is_junk(line: str, extra: list[str]) -> bool:
    if not re.search(r"[^\W_]", line) or _DROP_LINE.search(line):  # no letters/digits, or promo
        return True
    words = line.split()
    if words and len(_HASHTAG.findall(line)) >= max(1, len(words) // 2):
        return True  # mostly hashtags
    lowered = line.casefold()
    return any(term in lowered for term in extra)


def _cleaned(text: str, extra: list[str]) -> str:
    kept: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        # Match on the NFKC form so "𝗦𝗼𝗰𝗶𝗮𝗹𝘀"-style fancy Unicode headings look like plain text; keep the original.
        plain = unicodedata.normalize("NFKC", line)
        if _FOOTER_START.search(plain) or _FOOTER_ANYWHERE.search(plain):
            break
        if line.strip() and _is_junk(plain, extra):
            continue
        kept.append(line.rstrip())
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return out


def clean_description(text: str | None, mode: str, extra_terms: Iterable[str] = ()) -> str | None:
    """Description for the NFO according to the mapping's mode; None when nothing useful is left."""
    if not text or mode == "none":
        return None
    if mode == "full":
        return text
    extra = [t.strip().casefold() for t in extra_terms if t.strip()]
    cleaned = _cleaned(text, extra)
    if mode == "first_paragraph":
        cleaned = cleaned.split("\n\n", 1)[0].strip()
    return cleaned or None


def parse_terms(raw: str | None) -> list[str]:
    """The mapping's "also remove lines containing" box: one term per line or comma."""
    return [t.strip() for t in re.split(r"[\n,]", raw or "") if t.strip()]
