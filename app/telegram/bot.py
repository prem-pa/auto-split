"""Singleton ``Bot`` instance plus thin send/edit wrappers.

We deliberately use ``telegram.Bot`` directly (not ``Application``) because we
are webhook-only: FastAPI receives the update, we call ``Bot`` methods to
respond. No polling, no background workers.
"""

from __future__ import annotations

from functools import lru_cache

from telegram import Bot, InlineKeyboardMarkup, Message

from app.config import settings


@lru_cache(maxsize=1)
def get_bot() -> Bot:
    """Return the process-wide ``Bot`` singleton.

    Built lazily so importing this module does not require a real token —
    important for tests. Raises ``RuntimeError`` if the token setting is
    empty at first access.
    """
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set; cannot construct telegram.Bot"
        )
    return Bot(token=token)


async def send_message(
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    """Send a text message. Thin wrapper around ``Bot.send_message``."""
    return await get_bot().send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
    )


async def edit_message(
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    """Edit a previously-sent message's text (and optional keyboard).

    Telegram's ``edit_message_text`` returns either ``Message`` or ``True`` —
    the latter only happens for inline-mode messages, which we never use, so
    the cast below is safe.
    """
    result = await get_bot().edit_message_text(
        text=text,
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=reply_markup,
    )
    # In our usage (regular chat messages) Telegram always returns the edited
    # Message. The ``bool`` branch is only for inline-mode messages.
    assert isinstance(result, Message), (
        "edit_message_text returned bool for non-inline message"
    )
    return result
