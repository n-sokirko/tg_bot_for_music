"""Downloader logic with yt-dlp replaced by a fake (the real thing needs the network)."""

from pathlib import Path

import pytest
from yt_dlp.utils import DownloadError as YtDlpDownloadError

from musicbot import downloader as dl
from musicbot.config import Settings
from musicbot.downloader import Downloader, DownloadError, describe, safe_filename


def make(**overrides) -> Downloader:
    return Downloader(Settings(token="t", **overrides))


def test_describe_prefers_music_metadata():
    info = {"track": "Song", "title": "Song (Official Video)", "artists": ["A", "B"], "uploader": "A - Topic"}
    assert describe(info) == ("Song", "A, B")


def test_describe_falls_back_to_video_fields_and_strips_topic():
    assert describe({"title": "Some video", "uploader": "Chan"}) == ("Some video", "Chan")
    assert describe({"title": "x", "uploader": "Artist - Topic"}) == ("x", "Artist")
    assert describe({}) == ("Без названия", None)


def test_safe_filename():
    assert safe_filename('AC/DC: "Back" in Black?') == "AC_DC_ _Back_ in Black_"
    assert safe_filename("...") == "track"
    assert len(safe_filename("x" * 500)) == 120


def fake_extract(monkeypatch, *, info=None, write=b"mp3", error=None, seen=None):
    def extract(url, opts, download):
        if seen is not None:
            seen.update(url=url, opts=opts)
        if error:
            raise error
        # mimic yt-dlp: apply the match filter, otherwise write the mp3 it would produce
        reason = opts["match_filter"](info, incomplete=False)
        if reason is None and write is not None:
            Path(opts["outtmpl"].replace("%(id)s.%(ext)s", f"{info['id']}.mp3")).write_bytes(write)
        return info

    monkeypatch.setattr(dl, "_extract", extract)


INFO = {"id": "vid", "title": "T", "track": "Song", "artists": ["Artist"], "duration": 200.4}


async def test_download_track_ok(tmp_path, monkeypatch):
    seen = {}
    fake_extract(monkeypatch, info=INFO, seen=seen)
    track = await make().download_track("vid", tmp_path)

    assert (track.title, track.artist, track.duration) == ("Song", "Artist", 200)
    assert track.path == tmp_path / "vid.mp3"
    assert track.filename == "Artist - Song.mp3"
    assert seen["url"] == "https://www.youtube.com/watch?v=vid"
    assert seen["opts"]["noplaylist"] is True


async def test_download_track_rejects_long_video(tmp_path, monkeypatch):
    fake_extract(monkeypatch, info={**INFO, "duration": 31 * 60})
    with pytest.raises(DownloadError, match="30 мин"):
        await make(max_track_minutes=30).download_track("vid", tmp_path)


async def test_download_track_rejects_live(tmp_path, monkeypatch):
    fake_extract(monkeypatch, info={**INFO, "is_live": True})
    with pytest.raises(DownloadError, match="трансляция"):
        await make().download_track("vid", tmp_path)


async def test_download_track_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "TELEGRAM_UPLOAD_LIMIT", 2)
    fake_extract(monkeypatch, info=INFO, write=b"123")
    with pytest.raises(DownloadError, match="50 МБ"):
        await make().download_track("vid", tmp_path)


async def test_download_track_missing_output(tmp_path, monkeypatch):
    fake_extract(monkeypatch, info=INFO, write=None)
    with pytest.raises(DownloadError):
        await make().download_track("vid", tmp_path)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ERROR: Sign in to confirm you’re not a bot", "cookies"),
        ("ERROR: Private video. Sign in if you've been granted access", "приватное"),
        ("ERROR: Video unavailable", "недоступно"),
        ("ERROR: something totally new", "Не получилось"),
    ],
)
async def test_download_errors_are_translated(tmp_path, monkeypatch, message, expected):
    fake_extract(monkeypatch, error=YtDlpDownloadError(message))
    with pytest.raises(DownloadError, match=expected):
        await make().download_track("vid", tmp_path)


async def test_cookies_are_copied_per_call(tmp_path, monkeypatch):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    work = tmp_path / "work"
    work.mkdir()
    seen = {}
    fake_extract(monkeypatch, info=INFO, seen=seen)
    await make(cookies_file=str(cookies)).download_track("vid", work)
    assert seen["opts"]["cookiefile"] == str(work / "cookies.txt")
    assert (work / "cookies.txt").read_text() == cookies.read_text()


async def test_playlist_listing(monkeypatch):
    seen = {}

    def extract(url, opts, download):
        seen.update(url=url, opts=opts, download=download)
        return {
            "title": "Road trip",
            "playlist_count": 120,
            "entries": [
                {"id": "a", "title": "One"},
                None,
                {"id": "b", "title": "[Deleted video]"},
                {"id": "c", "title": "[Private video]"},
                {"id": "d"},
                {"title": "no id"},
            ],
        }

    monkeypatch.setattr(dl, "_extract", extract)
    playlist = await make(max_playlist_tracks=25).playlist("PL123")

    assert seen["url"] == "https://www.youtube.com/playlist?list=PL123"
    assert seen["download"] is False
    assert seen["opts"]["playlist_items"] == "1:25"
    assert playlist.title == "Road trip"
    assert [(e.video_id, e.title) for e in playlist.entries] == [("a", "One"), ("d", "Без названия")]
    assert playlist.total == 120


async def test_playlist_error_is_translated(monkeypatch):
    def extract(url, opts, download):
        raise YtDlpDownloadError("ERROR: The playlist does not exist.")

    monkeypatch.setattr(dl, "_extract", extract)
    with pytest.raises(DownloadError, match="не найден"):
        await make().playlist("PL123")
