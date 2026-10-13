from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    token: str
    allowed_users: frozenset[int] = frozenset()
    max_playlist_tracks: int = 50
    max_track_minutes: int = 30
    audio_bitrate: int = 192
    max_parallel_downloads: int = 2
    cookies_file: str | None = None


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} должно быть целым числом, а не {raw!r}") from None
    if value < 1:
        raise RuntimeError(f"{name} должно быть больше нуля")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    token = env.get("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Не задан BOT_TOKEN: получи токен у @BotFather и положи в .env")

    try:
        allowed = frozenset(int(x) for x in env.get("ALLOWED_USERS", "").replace(",", " ").split())
    except ValueError:
        raise RuntimeError("ALLOWED_USERS: ожидаю числовые Telegram id через запятую") from None

    return Settings(
        token=token,
        allowed_users=allowed,
        max_playlist_tracks=_positive_int(env, "MAX_PLAYLIST_TRACKS", 50),
        max_track_minutes=_positive_int(env, "MAX_TRACK_MINUTES", 30),
        audio_bitrate=_positive_int(env, "AUDIO_BITRATE", 192),
        max_parallel_downloads=_positive_int(env, "MAX_PARALLEL_DOWNLOADS", 2),
        cookies_file=env.get("COOKIES_FILE", "").strip() or None,
    )
