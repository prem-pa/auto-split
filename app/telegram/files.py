"""Download files attached to Telegram messages.

Telegram's file API is two-step:
    1. ``Bot.get_file(file_id)`` returns a ``File`` object with a temporary
       ``file_path`` URL (rooted at ``https://api.telegram.org/file/bot<TOKEN>/``).
    2. HTTP GET that URL to fetch the bytes.

python-telegram-bot's ``File.download_as_bytearray`` does both steps in one
call. We expose a thin ``async`` wrapper that returns ``bytes`` so callers in
Brick D (AI parsing) get a plain immutable buffer.
"""

from __future__ import annotations

from app.telegram.bot import get_bot


async def download_telegram_file(file_id: str) -> bytes:
    """Download a Telegram-hosted file by ``file_id``.

    Args:
        file_id: The ``file_id`` from an Update's photo / voice / document
            payload. Note: Telegram ``file_id`` values are valid for a
            limited time and per-bot; do not cache across bot tokens.

    Returns:
        Raw file bytes.
    """
    bot = get_bot()
    tg_file = await bot.get_file(file_id)
    buf = await tg_file.download_as_bytearray()
    return bytes(buf)
