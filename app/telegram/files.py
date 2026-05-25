"""Download files attached to Telegram messages.

Telegram's file API is two-step:
    1. ``Bot.get_file(file_id)`` returns a ``File`` object with a temporary
       ``file_path`` URL (rooted at ``https://api.telegram.org/file/bot<TOKEN>/``).
    2. HTTP GET that URL to fetch the bytes.

python-telegram-bot's ``File.download_as_bytearray`` does both steps in one
call. We expose a thin ``async`` wrapper that returns ``bytes`` so callers in
Brick D (AI parsing) get a plain immutable buffer.

Both steps reach out to ``api.telegram.org`` over the network, so they're
vulnerable to transient failures — DNS blips (``[Errno -3] Temporary failure
in name resolution``), connect timeouts, dropped reads. We retry those with
exponential backoff (:func:`app.retry.retry_async`) before giving up, so a
momentary network hiccup doesn't surface to the user as "I couldn't fetch your
photo." A 4xx (e.g. an expired ``file_id``) is not transient, so we fail fast.

The call is wrapped in ``@observe`` so a download failure shows up as an
errored span in Langfuse — without it, the failure happens before any LLM
span and is invisible in the trace.
"""

from __future__ import annotations

import httpx
from telegram.error import BadRequest, NetworkError

from app.observability import observe, update_span
from app.retry import retry_async
from app.telegram.bot import get_bot

# Transient connection-level failures we retry. ``httpx.TransportError`` covers
# ConnectError (incl. DNS "Temporary failure in name resolution"), ReadError,
# timeouts, etc. python-telegram-bot may instead wrap these as ``NetworkError``
# (which also covers ``TimedOut``). ``BadRequest`` is a NetworkError subclass
# but represents a 4xx (e.g. expired ``file_id``) — excluded so we fail fast.
_RETRY_ON = (httpx.TransportError, NetworkError)
_EXCLUDE = (BadRequest,)


@observe(name="download_telegram_file", capture_input=True, capture_output=False)
async def download_telegram_file(file_id: str) -> bytes:
    """Download a Telegram-hosted file by ``file_id``, retrying transient errors.

    Args:
        file_id: The ``file_id`` from an Update's photo / voice / document
            payload. Note: Telegram ``file_id`` values are valid for a
            limited time and per-bot; do not cache across bot tokens.

    Returns:
        Raw file bytes.

    Raises:
        The last transient network error after the retry budget is exhausted,
        or immediately on a non-transient error (e.g. ``BadRequest`` for an
        expired/invalid ``file_id``). Callers surface this as a user-facing
        message; ``@observe`` records it as an errored span.
    """
    bot = get_bot()

    async def _attempt() -> bytes:
        tg_file = await bot.get_file(file_id)
        return bytes(await tg_file.download_as_bytearray())

    data = await retry_async(
        _attempt,
        retry_on=_RETRY_ON,
        exclude=_EXCLUDE,
        name="telegram file download",
    )
    # Record size only, never the bytes themselves (privacy).
    update_span(metadata={"bytes": len(data)})
    return data
