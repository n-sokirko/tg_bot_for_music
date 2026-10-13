import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe, SetMyCommands
from aiogram.types import User

from musicbot.__main__ import check_bot


class Session(BaseSession):
    def __init__(self, error=None):
        super().__init__()
        self.error, self.calls = error, []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(type(method))
        if self.error:
            raise self.error(method=method, message="boom")
        return User(id=1, is_bot=True, first_name="Music", username="music_bot") if isinstance(method, GetMe) else True


async def test_check_bot_returns_username_and_sets_commands():
    session = Session()
    assert await check_bot(Bot("123:abc", session=session)) == "music_bot"
    assert session.calls == [GetMe, SetMyCommands]


async def test_bad_token_exits_with_hint():
    with pytest.raises(SystemExit, match="отклонил токен"):
        await check_bot(Bot("123:abc", session=Session(TelegramUnauthorizedError)))


async def test_unreachable_telegram_exits_with_hint():
    with pytest.raises(SystemExit, match="api.telegram.org"):
        await check_bot(Bot("123:abc", session=Session(TelegramNetworkError)))
