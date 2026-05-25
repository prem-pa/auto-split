"""Singleton ``Bot`` instance plus thin send/edit wrappers.

We deliberately use ``telegram.Bot`` directly (not ``Application``) because we
are webhook-only: FastAPI receives the update, we call ``Bot`` methods to
respond. No polling, no background workers.
"""

from __future__ import annotations

from functools import lru_cache

import httpx
from telegram import Bot, InlineKeyboardMarkup, Message
from telegram.error import BadRequest, NetworkError

from app.config import settings
from app.retry import retry_async

# Transient send/edit failures we retry. ``httpx.TransportError`` covers
# connection-level errors; PTB wraps some as ``NetworkError`` (incl.
# ``TimedOut``). ``BadRequest`` is a 4xx (chat not found, message not
# modified, …) — excluded so we don't retry a request that can't succeed.
_TG_RETRY_ON = (httpx.TransportError, NetworkError)
_TG_EXCLUDE = (BadRequest,)


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
    """Send a text message. Thin wrapper around ``Bot.send_message``.

    Retries transient network errors with backoff. A duplicate send on an
    ambiguous timeout is possible but low-harm (a repeated bot message), and
    far less likely than a clean connect failure that never reached Telegram.
    """
    return await retry_async(
        lambda: get_bot().send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
        ),
        retry_on=_TG_RETRY_ON,
        exclude=_TG_EXCLUDE,
        name="telegram send_message",
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
    # Editing is idempotent (sets the text to a fixed value), so retrying a
    # transient failure is safe.
    result = await retry_async(
        lambda: get_bot().edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        ),
        retry_on=_TG_RETRY_ON,
        exclude=_TG_EXCLUDE,
        name="telegram edit_message_text",
    )
    # In our usage (regular chat messages) Telegram always returns the edited
    # Message. The ``bool`` branch is only for inline-mode messages.
    assert isinstance(result, Message), (
        "edit_message_text returned bool for non-inline message"
    )
    return result
