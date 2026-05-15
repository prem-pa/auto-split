"""Phase-1 echo handlers.

Brick F (orchestrator) replaces these stubs with real expense-capture logic.
For now each handler just sends a short reply confirming what kind of input
the bot received — that's the smoke test for the webhook + signature +
Update parsing + send_message round trip.

All handlers are pure ``async`` functions that take a parsed ``Update``
and return ``None``. They are dispatched from
``app.telegram.webhook.handle_update``.
"""

from __future__ import annotations

import logging

from telegram import Update

from app.telegram.bot import send_message

log = logging.getLogger(__name__)


async def handle_text_message(update: Update) -> None:
    """Echo a text message: "got: {text}"."""
    message = update.message
    assert message is not None and message.text is not None  # router guarantees
    await send_message(chat_id=message.chat_id, text=f"got: {message.text}")


async def handle_photo_message(update: Update) -> None:
    """Reply "got a photo"."""
    message = update.message
    assert message is not None and message.photo  # router guarantees
    await send_message(chat_id=message.chat_id, text="got a photo")


async def handle_voice_message(update: Update) -> None:
    """Reply "got a voice note"."""
    message = update.message
    assert message is not None and message.voice is not None  # router guarantees
    await send_message(chat_id=message.chat_id, text="got a voice note")


async def handle_callback_query(update: Update) -> None:
    """Reply "got a tap on {callback_data}".

    Also answers the callback (clears the spinner on the user's button).
    Brick F replaces this with real confirm/edit/cancel routing.
    """
    cq = update.callback_query
    assert cq is not None  # router guarantees
    data = cq.data or ""
    # Acknowledge the tap so the client stops showing the loading state.
    try:
        await cq.answer()
    except Exception:  # noqa: BLE001 — best-effort; the echo reply is what matters
        log.exception("callback_query.answer() failed")
    if cq.message is not None:
        await send_message(chat_id=cq.message.chat_id, text=f"got a tap on {data}")
    else:
        log.warning("callback_query without an attached message: %s", data)


async def handle_unknown(update: Update) -> None:
    """Last-resort fallback for message types we don't echo specifically.

    Logged at INFO so we can see during testing what gets routed here.
    """
    log.info("unhandled update kind: %s", update.to_dict())
