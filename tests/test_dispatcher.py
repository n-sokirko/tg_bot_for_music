"""Updates pushed through the real Dispatcher: checks DI wiring, filters and middleware."""

from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.methods import EditMessageText, SendAudio, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from musicbot.app import build_dispatcher
from musicbot.config import Settings
from musicbot.downloader import Playlist, PlaylistEntry
from musicbot.handlers import PlaylistAction

from musicbot.handlers import PickTrack

from .test_handlers import CHAT, USER, FakeDownloader, FakeSearch, FakeSession, songs

# aiogram routers can be attached to one dispatcher only, so build it once.
PLAYLIST = Playlist("Mix", [PlaylistEntry("aaaaaaaaaaa", "One"), PlaylistEntry("bbbbbbbbbbb", "Two")], total=2)
DOWNLOADER = FakeDownloader(playlist=PLAYLIST)
SEARCH = FakeSearch(songs(3))
DP = build_dispatcher(Settings(token="t", allowed_users=frozenset({USER.id})), DOWNLOADER, SEARCH)


@pytest.fixture
def session():
    s = FakeSession()
    DOWNLOADER.requested.clear()
    return s


@pytest.fixture
def bot(session):
    return Bot("123:abc", session=session)


def text_update(text, user=USER, update_id=1):
    msg = Message(message_id=update_id, date=datetime.now(), chat=CHAT, from_user=user, text=text)
    return Update(update_id=update_id, message=msg)


async def test_track_link_end_to_end(bot, session):
    await DP.feed_update(bot, text_update("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RDx"))
    assert DOWNLOADER.requested == ["dQw4w9WgXcQ"]
    assert len(session.of(SendAudio)) == 1


async def test_start_command_mentions_limit(bot, session):
    await DP.feed_update(bot, text_update("/start"))
    assert "50" in session.of(SendMessage)[-1].text


async def test_strangers_are_turned_away(bot, session):
    stranger = User(id=1, is_bot=False, first_name="X")
    await DP.feed_update(bot, text_update("https://youtu.be/dQw4w9WgXcQ", user=stranger))
    assert DOWNLOADER.requested == []
    assert "приватный" in session.of(SendMessage)[-1].text


async def test_playlist_offer_then_button_press(bot, session):
    await DP.feed_update(bot, text_update("https://music.youtube.com/playlist?list=PL123", update_id=2))
    offer = session.of(EditMessageText)[-1]
    go_data = offer.reply_markup.inline_keyboard[0][0].callback_data
    assert PlaylistAction.unpack(go_data).action == "go"

    status = Message(message_id=99, date=datetime.now(), chat=CHAT, from_user=USER, text="offer")
    call = CallbackQuery(id="1", from_user=USER, chat_instance="c", message=status, data=go_data)
    await DP.feed_update(bot, Update(update_id=3, callback_query=call))

    assert DOWNLOADER.requested == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert len(session.of(SendAudio)) == 2
    assert "Отправлено 2 из 2" in session.of(EditMessageText)[-1].text


async def test_search_then_pick_end_to_end(bot, session):
    await DP.feed_update(bot, text_update("adele hello", update_id=4))
    assert SEARCH.queries[-1] == "adele hello"
    listing = session.of(EditMessageText)[-1]
    pick = listing.reply_markup.inline_keyboard[0][1].callback_data
    assert PickTrack.unpack(pick).video_id == "vid00000002"

    shown = Message(message_id=98, date=datetime.now(), chat=CHAT, from_user=USER, text="list")
    call = CallbackQuery(id="2", from_user=USER, chat_instance="c", message=shown, data=pick)
    await DP.feed_update(bot, Update(update_id=5, callback_query=call))

    assert DOWNLOADER.requested == ["vid00000002"]
    assert len(session.of(SendAudio)) == 1
