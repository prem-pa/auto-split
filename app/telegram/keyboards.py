"""Inline-keyboard builders.

Callback data convention (decoded by Brick F):
    - Confirm: ``cf:<pending_id_hex>``
    - Edit:    ``ed:<pending_id_hex>``
    - Cancel:  ``cx:<pending_id_hex>``

The 32-character hex form of a UUID is used (``UUID.hex``), giving a total
length of 2 + 1 + 32 = 35 bytes — well under Telegram's 64-byte limit.
"""

from __future__ import annotations

from uuid import UUID

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

CONFIRM_PREFIX = "cf"
EDIT_PREFIX = "ed"
CANCEL_PREFIX = "cx"

# Telegram limits callback_data to 64 bytes (UTF-8). Our format is always
# ASCII so byte length == character length.
_TELEGRAM_CALLBACK_DATA_LIMIT = 64


def _callback_data(prefix: str, pending_id: UUID) -> str:
    data = f"{prefix}:{pending_id.hex}"
    # Defensive: complains loudly if anyone ever tweaks the prefix scheme
    # in a way that overruns Telegram's limit.
    if len(data.encode("utf-8")) > _TELEGRAM_CALLBACK_DATA_LIMIT:
        raise ValueError(f"callback_data {data!r} exceeds Telegram's 64-byte limit")
    return data


def confirm_keyboard(pending_id: UUID) -> InlineKeyboardMarkup:
    """Single-button keyboard: Confirm."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Confirm", callback_data=_callback_data(CONFIRM_PREFIX, pending_id)
                )
            ]
        ]
    )


def edit_keyboard(pending_id: UUID) -> InlineKeyboardMarkup:
    """Single-button keyboard: Edit."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Edit", callback_data=_callback_data(EDIT_PREFIX, pending_id)
                )
            ]
        ]
    )


def cancel_keyboard(pending_id: UUID) -> InlineKeyboardMarkup:
    """Single-button keyboard: Cancel."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Cancel", callback_data=_callback_data(CANCEL_PREFIX, pending_id)
                )
            ]
        ]
    )


def confirmation_keyboard(pending_id: UUID) -> InlineKeyboardMarkup:
    """Combined Confirm / Edit / Cancel keyboard (one row).

    Convenience builder. Brick F (orchestrator) is free to call this or the
    three single-button builders above depending on the UX it wants.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Confirm", callback_data=_callback_data(CONFIRM_PREFIX, pending_id)
                ),
                InlineKeyboardButton(
                    "Edit", callback_data=_callback_data(EDIT_PREFIX, pending_id)
                ),
                InlineKeyboardButton(
                    "Cancel", callback_data=_callback_data(CANCEL_PREFIX, pending_id)
                ),
            ]
        ]
    )
