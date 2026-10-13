from __future__ import annotations

import asyncio
import html
import logging
import secrets
import tempfile
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from .config import Settings
from .downloader import Downloader, DownloadError, PlaylistEntry, Track
from .links import Link, find_links, has_url, is_video_id
from .search import Search, SearchResult

log = logging.getLogger(__name__)
router = Router()

MAX_LINKS_PER_MESSAGE = 3
# Stop a playlist after this many tracks in a row fail: it is almost always
# a systemic problem (YouTube blocking us), not bad luck with single videos.
MAX_CONSECUTIVE_FAILURES = 3
SEARCH_QUERY_MAX = 100
SEARCH_BUTTONS_PER_ROW = 4
UPLOAD_TIMEOUT = 600  # seconds; the default 60 is too short for a slow uplink
MAX_FAILURES_SHOWN = 10

GENERIC_ERROR = "Что-то пошло не так, попробуй ещё раз позже."


def esc(text: str) -> str:
    return html.escape(text, quote=False)


# --- state shared between handlers -----------------------------------------


@dataclass
class PendingPlaylist:
    owner_id: int
    title: str
    entries: list[PlaylistEntry]


class PendingStore:
    """Playlists waiting for the "download all" button (callback data is limited to 64 bytes)."""

    def __init__(self, capacity: int = 200) -> None:
        self._items: OrderedDict[str, PendingPlaylist] = OrderedDict()
        self._capacity = capacity

    def add(self, item: PendingPlaylist) -> str:
        token = secrets.token_urlsafe(6)
        self._items[token] = item
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)
        return token

    def pop(self, token: str, owner_id: int) -> PendingPlaylist | None:
        item = self._items.get(token)
        if item is None or item.owner_id != owner_id:
            return None
        return self._items.pop(token)


class ActiveJobs:
    """At most one running playlist per user, with a way to stop it."""

    def __init__(self) -> None:
        self._stops: dict[int, asyncio.Event] = {}

    def is_active(self, user_id: int) -> bool:
        return user_id in self._stops

    def start(self, user_id: int) -> asyncio.Event:
        self._stops[user_id] = event = asyncio.Event()
        return event

    def stop(self, user_id: int) -> bool:
        event = self._stops.get(user_id)
        if event is None:
            return False
        event.set()
        return True

    def finish(self, user_id: int) -> None:
        self._stops.pop(user_id, None)


class PlaylistAction(CallbackData, prefix="pl"):
    action: str  # go | no | stop
    token: str = ""


class PickTrack(CallbackData, prefix="pick"):
    video_id: str


class AccessMiddleware(BaseMiddleware):
    """Restrict the bot to ALLOWED_USERS when that list is not empty."""

    def __init__(self, allowed_users: frozenset[int]) -> None:
        self._allowed = allowed_users

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if not self._allowed or (user is not None and user.id in self._allowed):
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer("Этот бот приватный.")
        elif isinstance(event, CallbackQuery):
            await event.answer("Этот бот приватный.", show_alert=True)
        return None


# --- sending ---------------------------------------------------------------


async def _send_audio(bot: Bot, chat_id: int, track: Track) -> None:
    for attempt in range(3):
        try:
            await bot.send_audio(
                chat_id,
                FSInputFile(track.path, filename=track.filename),
                title=track.title,
                performer=track.artist,
                duration=track.duration,
                request_timeout=UPLOAD_TIMEOUT,
            )
            return
        except TelegramRetryAfter as exc:
            if attempt == 2:
                raise
            await asyncio.sleep(exc.retry_after)


async def deliver_track(bot: Bot, chat_id: int, video_id: str, downloader: Downloader) -> None:
    """Download one track and send it to the chat; the temp files are always removed."""
    with tempfile.TemporaryDirectory(prefix="tgmusic_") as tmp:
        track = await downloader.download_track(video_id, Path(tmp))
        try:
            await _send_audio(bot, chat_id, track)
        except TelegramForbiddenError:
            raise
        except TelegramAPIError as exc:
            log.warning("Telegram rejected %s: %s", video_id, exc)
            raise DownloadError("Telegram не принял файл.") from exc


async def _edit(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Edit a status message; failures here are cosmetic and never worth aborting for."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramAPIError:
        pass


# --- single track ----------------------------------------------------------


async def send_single_track(message: Message, bot: Bot, video_id: str, downloader: Downloader) -> None:
    status = await message.answer("⏳ Скачиваю…")
    try:
        await deliver_track(bot, message.chat.id, video_id, downloader)
    except DownloadError as exc:
        await _edit(status, f"❌ {esc(str(exc))}")
    except TelegramForbiddenError:
        return
    except Exception:
        log.exception("Track %s failed", video_id)
        await _edit(status, f"❌ {GENERIC_ERROR}")
    else:
        try:
            await status.delete()
        except TelegramAPIError:
            pass


# --- search by name --------------------------------------------------------


def _format_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    minutes, secs = divmod(seconds, 60)
    return f" · {minutes}:{secs:02d}"


def _results_text(query: str, results: list[SearchResult]) -> str:
    lines = [f"🔎 <b>{esc(query)}</b>\n"]
    for index, r in enumerate(results, 1):
        name = f"{esc(r.artist)} — {esc(r.title)}" if r.artist else esc(r.title)
        lines.append(f"{index}. {name}{_format_duration(r.duration)}")
    lines.append("\nВыбери номер, и я пришлю трек.")
    return "\n".join(lines)


def _results_keyboard(results: list[SearchResult]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=str(index), callback_data=PickTrack(video_id=r.video_id).pack())
        for index, r in enumerate(results, 1)
    ]
    step = SEARCH_BUTTONS_PER_ROW
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i : i + step] for i in range(0, len(buttons), step)])


async def search_tracks(message: Message, query: str, searcher: Search) -> None:
    status = await message.answer("🔎 Ищу…")
    try:
        results = await searcher.songs(query)
    except DownloadError as exc:
        await _edit(status, f"❌ {esc(str(exc))}")
        return
    except Exception:
        log.exception("Search %r failed", query)
        await _edit(status, f"❌ {GENERIC_ERROR}")
        return

    if not results:
        await _edit(status, f"Ничего не нашёл по запросу «{esc(query)}». Попробуй написать исполнителя и название.")
        return
    await _edit(status, _results_text(query, results), _results_keyboard(results))


# --- playlists -------------------------------------------------------------


def _offer_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⬇️ Скачать", callback_data=PlaylistAction(action="go", token=token).pack()),
                InlineKeyboardButton(text="Отмена", callback_data=PlaylistAction(action="no", token=token).pack()),
            ]
        ]
    )


_STOP_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="⏹ Стоп", callback_data=PlaylistAction(action="stop").pack())]]
)


async def offer_playlist(message: Message, playlist_id: str, user_id: int, downloader: Downloader, pending: PendingStore) -> None:
    status = await message.answer("🔎 Читаю плейлист…")
    try:
        playlist = await downloader.playlist(playlist_id)
    except DownloadError as exc:
        await _edit(status, f"❌ {esc(str(exc))}")
        return
    except Exception:
        log.exception("Playlist %s failed", playlist_id)
        await _edit(status, f"❌ {GENERIC_ERROR}")
        return

    if not playlist.entries:
        await _edit(status, "❌ В плейлисте нет доступных треков.")
        return

    token = pending.add(PendingPlaylist(owner_id=user_id, title=playlist.title, entries=playlist.entries))
    count = len(playlist.entries)
    text = f"📃 <b>{esc(playlist.title)}</b>\nТреков: {playlist.total}"
    if playlist.total > count:
        text += f" (скачаю первые {count})"
    await _edit(status, text, _offer_keyboard(token))


async def run_playlist(
    bot: Bot, status: Message, playlist: PendingPlaylist, downloader: Downloader, stop: asyncio.Event
) -> None:
    chat_id = status.chat.id
    title = esc(playlist.title)
    total = len(playlist.entries)
    sent = 0
    failed: list[str] = []
    streak = 0
    outcome = ""

    for index, entry in enumerate(playlist.entries, 1):
        if stop.is_set():
            outcome = "Остановлено."
            break
        if streak >= MAX_CONSECUTIVE_FAILURES:
            outcome = "Остановлено: несколько треков подряд не скачались."
            break

        await _edit(status, f"⏬ <b>{title}</b>\n{index}/{total}: {esc(entry.title)}", _STOP_KEYBOARD)
        try:
            await deliver_track(bot, chat_id, entry.video_id, downloader)
        except DownloadError as exc:
            failed.append(f"{entry.title}: {exc}")
            streak += 1
        except TelegramForbiddenError:
            return  # the user blocked the bot, nobody left to report to
        except Exception:
            log.exception("Track %s of playlist failed", entry.video_id)
            failed.append(f"{entry.title}: {GENERIC_ERROR}")
            streak += 1
        else:
            sent += 1
            streak = 0

    lines = [f"✅ <b>{title}</b>", f"Отправлено {sent} из {total}."]
    if outcome:
        lines.insert(1, outcome)
    if failed:
        lines.append("\nНе получилось:")
        lines += [f"• {esc(line)}" for line in failed[:MAX_FAILURES_SHOWN]]
        if len(failed) > MAX_FAILURES_SHOWN:
            lines.append(f"…и ещё {len(failed) - MAX_FAILURES_SHOWN}")
    await _edit(status, "\n".join(lines))


# --- handlers --------------------------------------------------------------


@router.message(CommandStart())
@router.message(Command("help"))
async def on_start(message: Message, settings: Settings) -> None:
    await message.answer(
        "👋 Я скачиваю музыку из YouTube Music и присылаю её аудиофайлом.\n\n"
        "🔎 <b>Поиск</b> — просто напиши название песни (лучше с исполнителем), "
        "выбери нужную из списка и получи mp3.\n"
        "🎵 <b>Трек</b> — или пришли ссылку на песню.\n"
        "📃 <b>Плейлист или альбом</b> — пришли ссылку на плейлист, и я отправлю треки по очереди "
        f"(до {settings.max_playlist_tracks} за раз).\n\n"
        "Ссылку можно взять через «Поделиться» в YouTube Music."
    )


@router.message(F.text)
async def on_text(
    message: Message, bot: Bot, downloader: Downloader, pending: PendingStore, searcher: Search
) -> None:
    if message.from_user is None:
        return
    # Links hidden behind text ("text_link" entities) are not visible in message.text.
    hidden = [e.url for e in message.entities or [] if e.type == "text_link" and e.url]
    links: list[Link] = find_links("\n".join([message.text or "", *hidden]))
    if not links:
        text = (message.text or "").strip()
        if has_url(text):
            await message.answer("Эту ссылку я не умею. Мне подойдут ссылки из YouTube Music и YouTube 🎧")
        elif text.startswith("/"):
            await message.answer("Не знаю такой команды. Напиши название песни или пришли ссылку 🎧")
        elif len(text) > SEARCH_QUERY_MAX:
            await message.answer(f"Слишком длинный запрос: уложись в {SEARCH_QUERY_MAX} символов.")
        else:
            await search_tracks(message, text, searcher)
        return

    for link in links[:MAX_LINKS_PER_MESSAGE]:
        if link.kind == "track":
            await send_single_track(message, bot, link.id, downloader)
        else:
            await offer_playlist(message, link.id, message.from_user.id, downloader, pending)


@router.callback_query(PickTrack.filter())
async def on_pick(call: CallbackQuery, callback_data: PickTrack, bot: Bot, downloader: Downloader) -> None:
    if not is_video_id(callback_data.video_id) or not isinstance(call.message, Message):
        await call.answer("Кнопка устарела, поищи заново.", show_alert=True)
        return
    await call.answer("⏳ Качаю…")
    await send_single_track(call.message, bot, callback_data.video_id, downloader)


@router.callback_query(PlaylistAction.filter(F.action == "no"))
async def on_playlist_cancel(call: CallbackQuery, callback_data: PlaylistAction, pending: PendingStore) -> None:
    if pending.pop(callback_data.token, call.from_user.id) is not None and isinstance(call.message, Message):
        await _edit(call.message, "Отменено.")
    await call.answer()


@router.callback_query(PlaylistAction.filter(F.action == "go"))
async def on_playlist_go(
    call: CallbackQuery,
    callback_data: PlaylistAction,
    bot: Bot,
    downloader: Downloader,
    pending: PendingStore,
    jobs: ActiveJobs,
) -> None:
    user_id = call.from_user.id
    if jobs.is_active(user_id):
        await call.answer("Уже качаю плейлист — дождись или нажми «Стоп».", show_alert=True)
        return
    playlist = pending.pop(callback_data.token, user_id)
    if playlist is None or not isinstance(call.message, Message):
        await call.answer("Кнопка устарела, пришли ссылку заново.", show_alert=True)
        return

    # Claim the user's slot before the first await, or two quick presses could both get through.
    stop = jobs.start(user_id)
    try:
        await call.answer()
        await run_playlist(bot, call.message, playlist, downloader, stop)
    finally:
        jobs.finish(user_id)


@router.callback_query(PlaylistAction.filter(F.action == "stop"))
async def on_playlist_stop(call: CallbackQuery, jobs: ActiveJobs) -> None:
    if jobs.stop(call.from_user.id):
        await call.answer("Остановлю после текущего трека.")
    else:
        await call.answer()
