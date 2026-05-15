"""Telegram dispatch handlers.

After Brick F (orchestrator) was wired in, these handlers are thin
adapters over :mod:`app.services.expense_pipeline`:

    * Message updates (text / photo / voice) all funnel into
      :func:`app.services.expense_pipeline.handle_incoming_message`,
      which decides what to do based on chat type and message contents.
    * Callback-query updates dispatch into
      :func:`app.services.expense_pipeline.handle_callback`.

The router in :mod:`app.telegram.webhook` still calls
``handle_text_message`` / ``handle_photo_message`` / ``handle_voice_message``
/ ``handle_callback_query`` / ``handle_unknown`` — we keep those four
names so the router's import surface is stable, but every one of them is
now a one-liner.
"""

from __future__ import annotations

import logging

from telegram import Update

from app.services import expense_pipeline
from app.telegram.bot import send_message  # noqa: F401 — re-exported for tests

log = logging.getLogger(__name__)


async def handle_text_message(update: Update) -> None:
    """Dispatch a text-only message into the orchestrator."""
    await expense_pipeline.handle_incoming_message(update)


async def handle_photo_message(update: Update) -> None:
    """Dispatch a photo (with or without caption / voice) into the orchestrator."""
    await expense_pipeline.handle_incoming_message(update)


async def handle_voice_message(update: Update) -> None:
    """Dispatch a voice-only message into the orchestrator."""
    await expense_pipeline.handle_incoming_message(update)


async def handle_callback_query(update: Update) -> None:
    """Dispatch an inline-keyboard tap into the orchestrator."""
    cq = update.callback_query
    if cq is None:  # router guarantees, but be defensive
        return
    await expense_pipeline.handle_callback(cq)


async def handle_unknown(update: Update) -> None:
    """Log + ignore message kinds we don't act on (stickers, video, etc.)."""
    log.info("unhandled update kind: %s", update.to_dict())
