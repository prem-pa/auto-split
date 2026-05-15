"""FastAPI router for Telegram webhook ingestion.

Responsibilities:
    1. Validate the ``X-Telegram-Bot-Api-Secret-Token`` header before reading
       the body. Reject with 403 on mismatch.
    2. Parse the body into a ``telegram.Update`` via ``Update.de_json``.
    3. Dispatch to the appropriate handler in ``app.telegram.handlers``.
    4. Always return 200 to Telegram (unless signature fails) — Telegram
       retries non-2xx responses, so handler errors must NOT bubble up as
       HTTP errors. Log them instead.

Privacy mode is ON in BotFather (see CLAUDE.md), so in groups we only
receive messages that @-mention the bot or reply to it. DMs see everything.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request, status
from telegram import Update

from app.config import settings
from app.telegram import handlers
from app.telegram.bot import get_bot

log = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram", tags=["telegram"])

# Header Telegram sends back to us (echoing the secret we passed at setWebhook).
_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def _verify_secret(provided: str | None) -> None:
    """Constant-time compare the secret header to ``settings.telegram_webhook_secret``.

    Raises ``HTTPException(403)`` on any mismatch (including missing header
    or unconfigured secret). We refuse to run with an empty secret in
    production — that would let anyone POST fake updates.
    """
    expected = settings.telegram_webhook_secret.get_secret_value()
    if not expected:
        # Refuse to accept any webhook traffic when the secret is unset.
        log.error("TELEGRAM_WEBHOOK_SECRET is empty; rejecting webhook request")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="webhook secret not configured",
        )
    if provided is None or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="invalid webhook secret"
        )


async def _dispatch(update: Update) -> None:
    """Route a parsed Update to the correct handler.

    Phase 1 is echo-only. Brick F swaps in the real pipeline.
    """
    if update.callback_query is not None:
        await handlers.handle_callback_query(update)
        return

    message = update.message
    if message is None:
        await handlers.handle_unknown(update)
        return

    if message.voice is not None:
        await handlers.handle_voice_message(update)
    elif message.photo:
        await handlers.handle_photo_message(update)
    elif message.text is not None:
        await handlers.handle_text_message(update)
    else:
        await handlers.handle_unknown(update)


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(
        default=None, alias=_SECRET_HEADER
    ),
) -> dict[str, str]:
    """Receive a Telegram update, verify it, parse it, dispatch."""
    _verify_secret(x_telegram_bot_api_secret_token)

    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 — Telegram should never send invalid JSON
        log.warning("malformed webhook payload: %s", exc)
        # Still 200 so Telegram doesn't retry the same garbage.
        return {"status": "ignored"}

    try:
        update = Update.de_json(payload, get_bot())
    except Exception:  # noqa: BLE001 — defensive; un-deserializable Update
        log.exception("Update.de_json failed; payload=%s", payload)
        return {"status": "ignored"}

    if update is None:
        log.warning("Update.de_json returned None; payload=%s", payload)
        return {"status": "ignored"}

    try:
        await _dispatch(update)
    except Exception:  # noqa: BLE001 — never let a handler error 5xx Telegram
        log.exception("handler raised while processing update_id=%s", update.update_id)

    return {"status": "ok"}
