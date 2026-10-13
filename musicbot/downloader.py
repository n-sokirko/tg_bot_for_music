"""Thin async wrapper around yt-dlp: fetch a playlist listing, download one track as mp3."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import YoutubeDLError

from .config import Settings

log = logging.getLogger(__name__)

# Bots may upload files up to 50 MB through the public Bot API.
TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024

_BAD_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_UNAVAILABLE_ENTRY_TITLES = {"[deleted video]", "[private video]"}

# Substring of the yt-dlp error (lowercase) -> what to tell the user.
_ERROR_HINTS = (
    ("not a bot", "YouTube заподозрил бота и просит войти в аккаунт. Администратору нужно добавить cookies (см. README)."),
    ("confirm your age", "Видео с возрастным ограничением, скачать не получится."),
    ("private video", "Видео приватное."),
    ("private playlist", "Плейлист приватный: открой к нему доступ по ссылке."),
    ("playlist does not exist", "Плейлист не найден или приватный."),
    ("members-only", "Видео только для участников канала."),
    ("country", "Видео недоступно в стране, где работает бот."),
    ("copyright", "Видео заблокировано правообладателем."),
    ("video unavailable", "Видео недоступно."),
    ("premiere", "Это ещё не вышедшая премьера."),
    ("live event", "Это трансляция, а не трек."),
)


class _YtdlpLogger:
    """Route yt-dlp's messages into our log.

    Warnings are what explain odd failures (an unsupported JS runtime, a missing
    format), so they must not be silenced. Errors are also raised as exceptions
    and routinely expected (private video), so they stay at debug level.
    """

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)


class DownloadError(Exception):
    """A failure whose message is safe to show to the user as is."""


@dataclass(frozen=True)
class Track:
    path: Path
    title: str
    artist: str | None
    duration: int | None

    @property
    def filename(self) -> str:
        name = f"{self.artist} - {self.title}" if self.artist else self.title
        return safe_filename(name) + self.path.suffix


@dataclass(frozen=True)
class PlaylistEntry:
    video_id: str
    title: str


@dataclass(frozen=True)
class Playlist:
    title: str
    entries: list[PlaylistEntry]
    total: int  # tracks in the whole playlist, may exceed len(entries)


def safe_filename(name: str, max_length: int = 120) -> str:
    cleaned = _BAD_FILENAME_CHARS.sub("_", name).strip(" .")
    return cleaned[:max_length].rstrip(" .") or "track"


def _explain(exc: Exception) -> str:
    message = str(exc).lower()
    for needle, hint in _ERROR_HINTS:
        if needle in message:
            return hint
    log.warning("Unrecognised yt-dlp error: %s", exc)
    return "Не получилось скачать: YouTube отказал или ссылка не работает."


def describe(info: dict[str, Any]) -> tuple[str, str | None]:
    """Pick a (title, artist) pair out of yt-dlp metadata.

    Official YouTube Music uploads carry `track`/`artists`; for ordinary videos
    we fall back to the video title and the channel name.
    """
    title = info.get("track") or info.get("title") or "Без названия"
    artists = info.get("artists")
    if artists:
        artist = ", ".join(artists)
    else:
        artist = info.get("artist") or info.get("creator") or info.get("uploader") or info.get("channel")
    if artist and artist.endswith(" - Topic"):
        artist = artist[: -len(" - Topic")]
    return title, artist or None


def _extract(url: str, opts: dict[str, Any], download: bool) -> dict[str, Any]:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=download)


class Downloader:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._slots = asyncio.Semaphore(settings.max_parallel_downloads)
        # yt-dlp enables only deno by default; use whatever JS runtime is installed.
        runtimes = {name: {} for name in ("deno", "node", "bun") if shutil.which(name)}
        self._js_runtimes = runtimes or None
        if self._js_runtimes is None:
            log.warning("No JS runtime (Node 22+, Deno 2.3+ or Bun) found: YouTube extraction may fail")

    def _base_opts(self, scratch: Path) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "logger": _YtdlpLogger(),
            "noprogress": True,
            "socket_timeout": 30,
            "retries": 3,
        }
        if self._js_runtimes:
            opts["js_runtimes"] = self._js_runtimes
        if self._settings.cookies_file:
            # yt-dlp writes cookies back on exit; give every call its own copy so
            # parallel downloads don't race and a read-only mount still works.
            cookies = scratch / "cookies.txt"
            shutil.copyfile(self._settings.cookies_file, cookies)
            opts["cookiefile"] = str(cookies)
        return opts

    async def playlist(self, playlist_id: str) -> Playlist:
        limit = self._settings.max_playlist_tracks
        url = f"https://www.youtube.com/playlist?list={playlist_id}"

        def run() -> dict[str, Any]:
            with tempfile.TemporaryDirectory(prefix="tgmusic_") as scratch:
                opts = {
                    **self._base_opts(Path(scratch)),
                    "extract_flat": "in_playlist",
                    "skip_download": True,
                    "playlist_items": f"1:{limit}",  # yt-dlp ranges are inclusive
                }
                return _extract(url, opts, download=False)

        try:
            info = await asyncio.to_thread(run)
        except YoutubeDLError as exc:
            raise DownloadError(_explain(exc)) from exc

        entries = [
            PlaylistEntry(video_id=entry["id"], title=entry.get("title") or "Без названия")
            for entry in (info.get("entries") or [])
            if entry
            and entry.get("id")
            and (entry.get("title") or "").lower() not in _UNAVAILABLE_ENTRY_TITLES
        ][:limit]
        total = max(info.get("playlist_count") or 0, len(entries))
        return Playlist(title=info.get("title") or "Плейлист", entries=entries, total=total)

    async def download_track(self, video_id: str, workdir: Path) -> Track:
        """Download one track into ``workdir`` as mp3 with tags and cover art."""
        max_seconds = self._settings.max_track_minutes * 60
        rejected: list[str] = []

        def match_filter(info: dict[str, Any], *, incomplete: bool = False) -> str | None:
            if info.get("is_live"):
                rejected.append("Это прямая трансляция, а не трек.")
            elif (info.get("duration") or 0) > max_seconds:
                rejected.append(f"Слишком длинное видео: лимит {self._settings.max_track_minutes} мин.")
            else:
                return None
            return rejected[-1]

        opts = {
            **self._base_opts(workdir),
            "format": "bestaudio/best",
            "outtmpl": str(workdir / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "writethumbnail": True,
            "match_filter": match_filter,
            "postprocessors": [
                {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": str(self._settings.audio_bitrate),
                },
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"},
            ],
        }
        url = f"https://www.youtube.com/watch?v={video_id}"

        async with self._slots:
            try:
                info = await asyncio.to_thread(_extract, url, opts, True)
            except YoutubeDLError as exc:
                raise DownloadError(_explain(exc)) from exc

        if rejected:
            raise DownloadError(rejected[0])

        path = workdir / f"{video_id}.mp3"
        if not path.is_file():
            log.error("yt-dlp finished but %s is missing", path)
            raise DownloadError("Не получилось скачать: файл не появился.")
        if path.stat().st_size > TELEGRAM_UPLOAD_LIMIT:
            raise DownloadError("Файл больше 50 МБ, Telegram не даст боту его отправить.")

        title, artist = describe(info)
        duration = int(info["duration"]) if info.get("duration") else None
        return Track(path=path, title=title, artist=artist, duration=duration)
