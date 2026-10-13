import pytest

from musicbot.config import load_settings


def test_defaults():
    s = load_settings({"BOT_TOKEN": " 123:abc "})
    assert s.token == "123:abc"
    assert s.allowed_users == frozenset()
    assert (s.max_playlist_tracks, s.max_track_minutes, s.audio_bitrate) == (50, 30, 192)
    assert s.cookies_file is None


def test_overrides():
    s = load_settings(
        {"BOT_TOKEN": "t", "ALLOWED_USERS": "1, 2 3", "MAX_PLAYLIST_TRACKS": "5", "COOKIES_FILE": "/c.txt", "MAX_TRACK_MINUTES": ""}
    )
    assert s.allowed_users == {1, 2, 3}
    assert s.max_playlist_tracks == 5
    assert s.max_track_minutes == 30  # blank means default
    assert s.cookies_file == "/c.txt"


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"BOT_TOKEN": "  "},
        {"BOT_TOKEN": "t", "ALLOWED_USERS": "me"},
        {"BOT_TOKEN": "t", "MAX_PLAYLIST_TRACKS": "many"},
        {"BOT_TOKEN": "t", "MAX_PLAYLIST_TRACKS": "0"},
    ],
)
def test_invalid_env(env):
    with pytest.raises(RuntimeError):
        load_settings(env)
