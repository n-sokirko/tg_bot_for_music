import pytest

from musicbot.links import Link, find_links, parse_link

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    [
        f"https://music.youtube.com/watch?v={VID}",
        f"https://music.youtube.com/watch?v={VID}&si=abc123",
        f"https://www.youtube.com/watch?v={VID}&t=30s",
        f"https://m.youtube.com/watch?v={VID}",
        f"https://youtu.be/{VID}",
        f"https://youtu.be/{VID}?si=xyz",
        # opened from an album/radio: the shared thing is still the song
        f"https://music.youtube.com/watch?v={VID}&list=OLAK5uy_abcdef",
    ],
)
def test_track_links(url):
    assert parse_link(url) == Link("track", VID)


@pytest.mark.parametrize(
    ("url", "playlist_id"),
    [
        ("https://music.youtube.com/playlist?list=PLabc_123-XYZ", "PLabc_123-XYZ"),
        ("https://www.youtube.com/playlist?list=OLAK5uy_kAbC&si=q", "OLAK5uy_kAbC"),
    ],
)
def test_playlist_links(url, playlist_id):
    assert parse_link(url) == Link("playlist", playlist_id)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/watch?v=" + VID,
        "https://evil.com/?u=https://music.youtube.com/watch?v=" + VID,
        "https://music.youtube.com.evil.com/watch?v=" + VID,
        "https://soundcloud.com/artist/track",
        "https://music.youtube.com/watch?v=short",
        "https://music.youtube.com/watch",
        "https://music.youtube.com/playlist",
        "https://music.youtube.com/playlist?list=",
        "https://music.youtube.com/browse/MPREb_abc",
        "https://www.youtube.com/@channel",
        "https://youtu.be/",
        "not a url",
        "http://[::1",
    ],
)
def test_rejected_links(url):
    assert parse_link(url) is None


def test_find_links_in_text():
    text = (
        f"глянь (https://music.youtube.com/watch?v={VID}), и ещё "
        "https://music.youtube.com/playlist?list=PLabc123."
    )
    assert find_links(text) == [Link("track", VID), Link("playlist", "PLabc123")]


def test_find_links_dedupes_and_ignores_noise():
    text = f"https://youtu.be/{VID} https://music.youtube.com/watch?v={VID} https://example.com/x"
    assert find_links(text) == [Link("track", VID)]
    assert find_links("просто текст") == []
