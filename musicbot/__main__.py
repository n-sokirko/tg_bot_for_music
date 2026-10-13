from __future__ import annotations

import asyncio
import logging
import shutil
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from dotenv import load_dotenv

from .app import build_dispatcher
from .config import load_settings
from .downloader import Downloader


async def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg не найден: установи его (apt install ffmpeg / brew install ffmpeg)")
    try:
        settings = load_settings()
    except RuntimeError as exc:
        sys.exit(str(exc))

    bot = Bot(settings.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(settings, Downloader(settings))

    await bot.set_my_commands(
        [BotCommand(command="start", description="Как пользоваться"), BotCommand(command="help", description="Помощь")]
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
