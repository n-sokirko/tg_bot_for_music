"""Search YouTube Music by song name.

yt-dlp's own YouTube Music search only returns ids and titles (no artist, no
duration), which is useless for telling apart two songs with the same name,
so the listing comes from ytmusicapi and yt-dlp only does the downloading.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any

from .downloader import DownloadError

log = logging.getLogger(__name__)

SEARCH_RESULTS = 8


@dataclass(frozen=True)
class SearchResult:
    video_id: str
    title: str
    artist: str | None
    duration: int | None  # seconds


class Search:
    def __init__(self, max_track_minutes: int, client: Any = None) -> None:
        self._max_seconds = max_track_minutes * 60
        self._client = client
        self._lock = threading.Lock()

    def _get_client(self) -> Any:
        # Created on first use so the bot can start (and report problems) without network.
        with self._lock:
            if self._client is None:
                from ytmusicapi import YTMusic

                self._client = YTMusic()
            return self._client

    async def songs(self, query: str) -> list[SearchResult]:
        try:
            raw = await asyncio.to_thread(
                lambda: self._get_client().search(query, filter="songs", limit=SEARCH_RESULTS)
            )
        except Exception as exc:
            log.warning("YouTube Music search failed: %s", exc)
            raise DownloadError("Поиск сейчас недоступен. Попробуй позже или пришли ссылку на трек.") from exc

        results: list[SearchResult] = []
        for item in raw:
            video_id, title = item.get("videoId"), item.get("title")
            if not video_id or not title:
                continue  # not a playable song (a stray album or artist card)
            duration = item.get("duration_seconds")
            if duration and duration > self._max_seconds:
                continue  # the downloader would refuse it anyway
            names = [a["name"] for a in item.get("artists") or [] if a.get("name")]
            results.append(SearchResult(video_id, title, ", ".join(names) or None, duration))
        return results[:SEARCH_RESULTS]
