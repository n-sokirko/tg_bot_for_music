from __future__ import annotations

from aiogram import Dispatcher

from .config import Settings
from .downloader import Downloader
from .handlers import AccessMiddleware, ActiveJobs, PendingStore, router


def build_dispatcher(settings: Settings, downloader: Downloader) -> Dispatcher:
    # Keyword arguments become workflow data: handlers receive them by parameter name.
    dp = Dispatcher(settings=settings, downloader=downloader, pending=PendingStore(), jobs=ActiveJobs())

    access = AccessMiddleware(settings.allowed_users)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_router(router)
    return dp
