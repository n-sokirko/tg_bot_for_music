"""Pull YouTube / YouTube Music links out of free text.

Only a fixed set of YouTube hosts is accepted. yt-dlp can fetch from
thousands of sites, and we do not want the bot to be a generic downloader
(or a way to make the server request arbitrary URLs).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlparse

_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_SHORT_HOST = "youtu.be"

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_VIDEO_ID_RE = re.compile(r"[\w-]{11}")
_PLAYLIST_ID_RE = re.compile(r"[\w-]{2,64}")
_TRAILING_PUNCTUATION = ".,;:!?)]}»"


@dataclass(frozen=True)
class Link:
    kind: Literal["track", "playlist"]
    id: str


def is_video_id(value: str) -> bool:
    return bool(_VIDEO_ID_RE.fullmatch(value))


def has_url(text: str) -> bool:
    return _URL_RE.search(text) is not None


def parse_link(url: str) -> Link | None:
    try:
        parts = urlparse(url)
        host = parts.hostname or ""
    except ValueError:
        return None

    if host == _SHORT_HOST:
        video_id = parts.path.lstrip("/").split("/")[0]
        return Link("track", video_id) if _VIDEO_ID_RE.fullmatch(video_id) else None

    if host not in _YOUTUBE_HOSTS:
        return None

    query = parse_qs(parts.query)
    if parts.path == "/watch":
        # A track opened from an album or radio carries a list= too, but the
        # person shared a song, so the playlist part is deliberately ignored.
        video_id = query.get("v", [""])[0]
        return Link("track", video_id) if _VIDEO_ID_RE.fullmatch(video_id) else None
    if parts.path == "/playlist":
        playlist_id = query.get("list", [""])[0]
        return Link("playlist", playlist_id) if _PLAYLIST_ID_RE.fullmatch(playlist_id) else None
    return None


def find_links(text: str) -> list[Link]:
    """All supported links in ``text``, in order, without duplicates."""
    found: list[Link] = []
    for match in _URL_RE.finditer(text):
        link = parse_link(match.group().rstrip(_TRAILING_PUNCTUATION))
        if link and link not in found:
            found.append(link)
    return found
