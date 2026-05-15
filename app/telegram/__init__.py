"""Brick B — Telegram I/O surface.

Public interface for other bricks to import:

    from app.telegram import (
        telegram_router,
        send_message,
        edit_message,
        download_telegram_file,
        keyboards,
    )
"""

from app.telegram import keyboards
from app.telegram.bot import edit_message, get_bot, send_message
from app.telegram.files import download_telegram_file
from app.telegram.webhook import router as telegram_router

__all__ = [
    "download_telegram_file",
    "edit_message",
    "get_bot",
    "keyboards",
    "send_message",
    "telegram_router",
]
