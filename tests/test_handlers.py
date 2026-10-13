"""Bot flows against a fake Telegram session that records every API call."""

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import DeleteMessage, EditMessageText, SendAudio, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, User

from musicbot.downloader import DownloadError, Playlist, PlaylistEntry, Track
from musicbot.search import SearchResult
from musicbot.handlers import (
    AccessMiddleware,
    ActiveJobs,
    PendingPlaylist,
    PendingStore,
    PickTrack,
    PlaylistAction,
    on_pick,
    on_playlist_go,
    on_text,
    run_playlist,
    send_single_track,
)

CHAT = Chat(id=42, type="private")
USER = User(id=7, is_bot=False, first_name="Test")


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.on_send_audio = None

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendAudio) and self.on_send_audio:
            self.on_send_audio(method)
        if isinstance(method, DeleteMessage):
            return True
        # the real session binds returned objects to the bot, so replies can be edited/deleted
        return Message(message_id=len(self.calls), date=datetime.now(), chat=CHAT, from_user=USER).as_(bot)

    def of(self, kind):
        return [c for c in self.calls if isinstance(c, kind)]


class FakeDownloader:
    """Writes a tiny file per track; ids listed in `broken` fail."""

    def __init__(self, broken=(), playlist=None):
        self.broken = set(broken)
        self.requested: list[str] = []
        self._playlist = playlist

    async def download_track(self, video_id, workdir: Path) -> Track:
        self.requested.append(video_id)
        if video_id in self.broken:
            raise DownloadError(f"{video_id} недоступно")
        path = workdir / f"{video_id}.mp3"
        path.write_bytes(b"x")
        return Track(path=path, title=f"Song {video_id}", artist="Band", duration=180)

    async def playlist(self, playlist_id):
        if self._playlist is None:
            raise DownloadError("Плейлист не найден или приватный.")
        return self._playlist


class FakeSearch:
    def __init__(self, results=(), error=None):
        self.results, self.error, self.queries = list(results), error, []

    async def songs(self, query):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.results


def songs(n):
    return [SearchResult(f"vid{i:08d}", f"Song {i}", "Band" if i % 2 else None, 60 * i + 5) for i in range(1, n + 1)]


@pytest.fixture
def session():
    return FakeSession()


@pytest.fixture
def bot(session):
    return Bot("123:abc", session=session)


def entries(*ids):
    return [PlaylistEntry(video_id=i, title=f"Title {i}") for i in ids]


def pending(*ids):
    return PendingPlaylist(owner_id=USER.id, title="My <mix>", entries=entries(*ids))


def message(bot, text="hi", **kwargs):
    return Message(message_id=1, date=datetime.now(), chat=CHAT, from_user=USER, text=text, **kwargs).as_(bot)


async def test_single_track_is_sent_and_status_removed(bot, session):
    sent = []
    session.on_send_audio = sent.append
    await send_single_track(message(bot), bot, "abc", FakeDownloader())

    (audio,) = sent
    assert (audio.title, audio.performer, audio.duration) == ("Song abc", "Band", 180)
    assert audio.audio.filename == "Band - Song abc.mp3"
    assert len(session.of(DeleteMessage)) == 1  # "downloading…" status is cleaned up


async def test_single_track_failure_is_reported(bot, session):
    await send_single_track(message(bot), bot, "bad", FakeDownloader(broken={"bad"}))
    assert not session.of(SendAudio)
    assert "bad недоступно" in session.of(EditMessageText)[-1].text


async def test_unexpected_error_does_not_leak_details(bot, session):
    class Boom(FakeDownloader):
        async def download_track(self, *a):
            raise RuntimeError("secret internal detail")

    await send_single_track(message(bot), bot, "abc", Boom())
    text = session.of(EditMessageText)[-1].text
    assert "secret" not in text and "❌" in text


async def test_playlist_sends_all_and_reports_failures(bot, session):
    status = message(bot)
    downloader = FakeDownloader(broken={"b"})
    await run_playlist(bot, status, pending("a", "b", "c"), downloader, asyncio.Event())

    assert downloader.requested == ["a", "b", "c"]
    assert len(session.of(SendAudio)) == 2
    final = session.of(EditMessageText)[-1].text
    assert "Отправлено 2 из 3" in final
    assert "Title b: b недоступно" in final
    assert "My &lt;mix&gt;" in final  # titles are HTML-escaped


async def test_playlist_aborts_after_consecutive_failures(bot, session):
    downloader = FakeDownloader(broken={"a", "b", "c", "d"})
    await run_playlist(bot, message(bot), pending("a", "b", "c", "d", "e"), downloader, asyncio.Event())

    assert downloader.requested == ["a", "b", "c"]  # gave up before d
    final = session.of(EditMessageText)[-1].text
    assert "подряд" in final and "Отправлено 0 из 5" in final


async def test_playlist_stop_between_tracks(bot, session):
    stop = asyncio.Event()
    session.on_send_audio = lambda _: stop.set()  # user presses Stop while track 1 is uploading
    downloader = FakeDownloader()
    await run_playlist(bot, message(bot), pending("a", "b", "c"), downloader, stop)

    assert downloader.requested == ["a"]
    final = session.of(EditMessageText)[-1].text
    assert "Остановлено" in final and "Отправлено 1 из 3" in final


async def test_playlist_ends_quietly_if_user_blocked_bot(bot, session):
    def forbid(method):
        raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")

    session.on_send_audio = forbid
    downloader = FakeDownloader()
    await run_playlist(bot, message(bot), pending("a", "b"), downloader, asyncio.Event())
    assert downloader.requested == ["a"]


def test_pending_store_is_owner_scoped_and_bounded():
    store = PendingStore(capacity=2)
    first = store.add(pending("a"))
    store.add(pending("b"))
    store.add(pending("c"))  # evicts the oldest
    assert store.pop(first, USER.id) is None

    token = store.add(pending("d"))
    assert store.pop(token, owner_id=999) is None  # someone else's button
    assert store.pop(token, USER.id) is not None
    assert store.pop(token, USER.id) is None  # single use


def test_active_jobs():
    jobs = ActiveJobs()
    assert not jobs.stop(1)
    stop = jobs.start(1)
    assert jobs.is_active(1) and jobs.stop(1) and stop.is_set()
    jobs.finish(1)
    assert not jobs.is_active(1)


async def test_text_with_playlist_link_offers_buttons(bot, session):
    downloader = FakeDownloader(playlist=Playlist("Road <trip>", entries("a", "b"), total=80))
    store = PendingStore()
    await on_text(message(bot, "https://music.youtube.com/playlist?list=PL123"), bot, downloader, store, FakeSearch())

    offer = session.of(EditMessageText)[-1]
    assert "Road &lt;trip&gt;" in offer.text and "Треков: 80 (скачаю первые 2)" in offer.text
    buttons = [b.callback_data for row in offer.reply_markup.inline_keyboard for b in row]
    assert all(len(b.encode()) <= 64 for b in buttons)
    go = PlaylistAction.unpack(buttons[0])
    assert go.action == "go" and store.pop(go.token, USER.id) is not None


async def ask(bot, text, searcher, downloader=None):
    await on_text(message(bot, text), bot, downloader or FakeDownloader(), PendingStore(), searcher)


async def test_plain_text_is_searched_and_listed(bot, session):
    searcher = FakeSearch(songs(8))
    await ask(bot, "  adele hello ", searcher)

    assert searcher.queries == ["adele hello"]
    listing = session.of(EditMessageText)[-1]
    assert "1. Band — Song 1 · 1:05" in listing.text
    assert "2. Song 2 · 2:05" in listing.text  # no artist known
    rows = listing.reply_markup.inline_keyboard
    assert [[b.text for b in row] for row in rows] == [["1", "2", "3", "4"], ["5", "6", "7", "8"]]
    data = [b.callback_data for row in rows for b in row]
    assert all(len(d.encode()) <= 64 for d in data)
    assert PickTrack.unpack(data[2]).video_id == "vid00000003"


async def test_search_results_are_html_escaped(bot, session):
    await ask(bot, "a <b>", FakeSearch([SearchResult("vid00000001", "<i>x</i>", "A&B", 61)]))
    text = session.of(EditMessageText)[-1].text
    assert "&lt;b&gt;" in text and "A&amp;B — &lt;i&gt;x&lt;/i&gt;" in text


async def test_search_without_results(bot, session):
    await ask(bot, "asdkjhasd", FakeSearch([]))
    assert "Ничего не нашёл" in session.of(EditMessageText)[-1].text


async def test_search_failure_is_reported(bot, session):
    await ask(bot, "adele", FakeSearch(error=DownloadError("Поиск сейчас недоступен.")))
    assert "Поиск сейчас недоступен" in session.of(EditMessageText)[-1].text
    await ask(bot, "adele", FakeSearch(error=RuntimeError("secret")))
    assert "secret" not in session.of(EditMessageText)[-1].text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://open.spotify.com/track/abc", "ссылку я не умею"),
        ("/nope", "Не знаю такой команды"),
        ("x" * 101, "Слишком длинный"),
    ],
)
async def test_text_that_is_not_a_query(bot, session, text, expected):
    searcher = FakeSearch(songs(3))
    await ask(bot, text, searcher)
    assert searcher.queries == []
    assert expected in session.of(SendMessage)[-1].text


async def test_picking_a_result_sends_that_track(bot, session):
    class Call:
        from_user = USER
        message = Message(message_id=5, date=datetime.now(), chat=CHAT, from_user=USER, text="list").as_(bot)
        answers = []

        async def answer(self, text=None, show_alert=False):
            self.answers.append(text)

    downloader = FakeDownloader()
    await on_pick(Call(), PickTrack(video_id="vid00000003"), bot, downloader)
    assert downloader.requested == ["vid00000003"]
    assert len(session.of(SendAudio)) == 1


async def test_forged_pick_is_rejected(bot):
    answers = []

    class Call:
        from_user = USER
        message = Message(message_id=5, date=datetime.now(), chat=CHAT, from_user=USER, text="list").as_(bot)

        async def answer(self, text=None, show_alert=False):
            answers.append(text)

    downloader = FakeDownloader()
    await on_pick(Call(), PickTrack(video_id="../../etc"), bot, downloader)
    assert downloader.requested == [] and "устарела" in answers[0]


async def test_hidden_text_link_is_picked_up(bot, session):
    from aiogram.types import MessageEntity

    entity = MessageEntity(type="text_link", offset=0, length=4, url="https://youtu.be/dQw4w9WgXcQ")
    downloader = FakeDownloader()
    await on_text(message(bot, "ссыль", entities=[entity]), bot, downloader, PendingStore(), FakeSearch())
    assert downloader.requested == ["dQw4w9WgXcQ"]


async def test_unreadable_playlist_reports_error(bot, session):
    await on_text(message(bot, "https://music.youtube.com/playlist?list=PL123"), bot, FakeDownloader(), PendingStore(), FakeSearch())
    assert "не найден" in session.of(EditMessageText)[-1].text


async def test_second_playlist_is_refused_while_one_runs(bot, session):
    answers = []

    class Call:
        from_user = USER
        message = None

        async def answer(self, text=None, show_alert=False):
            answers.append(text)

    jobs = ActiveJobs()
    jobs.start(USER.id)
    await on_playlist_go(Call(), PlaylistAction(action="go", token="t"), bot, FakeDownloader(), PendingStore(), jobs)
    assert answers and "Уже качаю" in answers[0]


async def test_expired_button_is_handled(bot):
    answers = []

    class Call:
        from_user = USER
        message = None

        async def answer(self, text=None, show_alert=False):
            answers.append(text)

    await on_playlist_go(Call(), PlaylistAction(action="go", token="gone"), bot, FakeDownloader(), PendingStore(), ActiveJobs())
    assert "устарела" in answers[0]


@pytest.mark.parametrize(("allowed", "user_id", "passes"), [((), 5, True), ((5,), 5, True), ((5,), 6, False)])
async def test_access_middleware(bot, session, allowed, user_id, passes):
    reached = []

    async def handler(event, data):
        reached.append(event)

    msg = Message(
        message_id=1, date=datetime.now(), chat=CHAT, text="x", from_user=User(id=user_id, is_bot=False, first_name="U")
    ).as_(bot)
    await AccessMiddleware(frozenset(allowed))(handler, msg, {})
    assert bool(reached) is passes
    assert bool(session.of(SendMessage)) is (not passes)


async def test_two_simultaneous_presses_start_only_one_playlist(bot, session):
    release = asyncio.Event()
    answers = []

    class SlowDownloader(FakeDownloader):
        async def download_track(self, video_id, workdir):
            await release.wait()
            return await super().download_track(video_id, workdir)

    def call_for(token):
        class Call:
            from_user = USER
            message = Message(message_id=5, date=datetime.now(), chat=CHAT, from_user=USER, text="offer").as_(bot)

            async def answer(self, text=None, show_alert=False):
                answers.append(text)
                await asyncio.sleep(0)  # a real answer is a network round trip

        return Call()

    store, jobs, downloader = PendingStore(), ActiveJobs(), SlowDownloader()
    t1, t2 = store.add(pending("a")), store.add(pending("b"))

    async def press(token):
        await on_playlist_go(call_for(token), PlaylistAction(action="go", token=token), bot, downloader, store, jobs)

    first = asyncio.create_task(press(t1))
    second = asyncio.create_task(press(t2))
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(first, second)

    assert downloader.requested == ["a"]  # the second playlist never started
    assert any(a and "Уже качаю" in a for a in answers)
    assert not jobs.is_active(USER.id)
