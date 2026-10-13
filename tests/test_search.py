"""Search wrapper with ytmusicapi replaced by a fake client (the real one needs the network)."""

import pytest

from musicbot.downloader import DownloadError
from musicbot.search import SEARCH_RESULTS, Search


class FakeClient:
    def __init__(self, results=None, error=None):
        self.results, self.error, self.calls = results or [], error, []

    def search(self, query, filter=None, limit=None):
        self.calls.append((query, filter, limit))
        if self.error:
            raise self.error
        return self.results


def song(video_id="v1", title="Hello", artists=("Adele",), seconds=295):
    return {
        "resultType": "song",
        "videoId": video_id,
        "title": title,
        "artists": [{"name": a, "id": None} for a in artists],
        "duration_seconds": seconds,
    }


async def test_maps_ytmusicapi_results():
    client = FakeClient([song(), song("v2", "Duet", ("A", "B"), 100)])
    results = await Search(30, client).songs("adele hello")

    assert client.calls == [("adele hello", "songs", SEARCH_RESULTS)]
    assert [(r.video_id, r.title, r.artist, r.duration) for r in results] == [
        ("v1", "Hello", "Adele", 295),
        ("v2", "Duet", "A, B", 100),
    ]


async def test_skips_unplayable_and_too_long_items():
    client = FakeClient(
        [
            {"resultType": "song", "title": "no id"},
            {"resultType": "song", "videoId": "v0"},
            song("v1", seconds=31 * 60),
            song("v2", artists=()),
            song("v3", seconds=None),
        ]
    )
    results = await Search(30, client).songs("x")
    assert [(r.video_id, r.artist, r.duration) for r in results] == [("v2", None, 295), ("v3", "Adele", None)]


async def test_caps_number_of_results():
    client = FakeClient([song(f"v{i}") for i in range(20)])
    assert len(await Search(30, client).songs("x")) == SEARCH_RESULTS


async def test_backend_errors_become_user_errors():
    with pytest.raises(DownloadError, match="Поиск сейчас недоступен") as info:
        await Search(30, FakeClient(error=RuntimeError("HTTP 500 secret")) ).songs("x")
    assert "secret" not in str(info.value)


async def test_real_client_is_created_lazily(monkeypatch):
    import ytmusicapi

    made = []
    monkeypatch.setattr(ytmusicapi, "YTMusic", lambda: made.append(1) or FakeClient([song()]))
    search = Search(30)
    assert made == []  # nothing happens at construction time
    await search.songs("x")
    await search.songs("y")
    assert made == [1]  # created once, then reused
