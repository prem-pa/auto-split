"""CRUD for ``expenses_pending`` and ``expenses_completed``.

Lifecycle:
    create_pending(...) -> ExpensePending
        |
        v
    (user taps Confirm)
        |
        v
    mark_completed(pending_id, splitwise_expense_id) -> ExpenseCompleted
        - copies fields from the pending row,
        - inserts into ``expenses_completed``,
        - deletes the pending row.

    sweep_expired_pending() purges abandoned confirmations whose
    ``expires_at`` is in the past (Brick F schedules this).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.db.client import get_supabase
from app.db.models import ExpenseCompleted, ExpensePending

_PENDING_TABLE = "expenses_pending"
_COMPLETED_TABLE = "expenses_completed"


class PendingNotFoundError(LookupError):
    """Raised when a pending expense id does not resolve."""


async def create_pending(
    *,
    telegram_message_id: int | None,
    telegram_group_id: int | None,
    payer_telegram_user_id: int | None,
    parsed_data: dict[str, Any],
    receipt_image_url: str | None = None,
    expires_at: datetime | None = None,
) -> ExpensePending:
    """Insert a pending expense.

    ``expires_at`` defaults to the DB-side ``NOW() + 10 minutes``.
    Callers may pass an explicit value (e.g. for tests).
    """

    def _q() -> ExpensePending:
        client = get_supabase()
        payload: dict[str, Any] = {
            "telegram_message_id": telegram_message_id,
            "telegram_group_id": telegram_group_id,
            "payer_telegram_user_id": payer_telegram_user_id,
            "parsed_data": parsed_data,
            "receipt_image_url": receipt_image_url,
        }
        if expires_at is not None:
            payload["expires_at"] = expires_at.isoformat()
        resp = client.table(_PENDING_TABLE).insert(payload).execute()
        rows = resp.data or []
        if not rows:
            raise RuntimeError("create_pending: Supabase returned no rows")
        return ExpensePending.model_validate(rows[0])

    return await asyncio.to_thread(_q)


async def get_pending(id: UUID) -> ExpensePending | None:
    """Fetch a pending expense by id, or ``None`` if absent."""

    def _q() -> ExpensePending | None:
        client = get_supabase()
        resp = (
            client.table(_PENDING_TABLE)
            .select("*")
            .eq("id", str(id))
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return None
        return ExpensePending.model_validate(rows[0])

    return await asyncio.to_thread(_q)


async def delete_pending(id: UUID) -> None:
    """Delete a pending expense by id. No-op if not present."""

    def _q() -> None:
        client = get_supabase()
        client.table(_PENDING_TABLE).delete().eq("id", str(id)).execute()

    await asyncio.to_thread(_q)


async def mark_completed(
    pending_id: UUID,
    splitwise_expense_id: int,
    *,
    amount_cents: int | None = None,
    currency: str | None = None,
) -> ExpenseCompleted:
    """Move a pending expense into ``expenses_completed``.

    Reads the pending row, inserts a corresponding completed row with
    the supplied Splitwise expense id, then deletes the pending row.
    ``amount_cents`` and ``currency`` may be passed explicitly; if
    omitted, they are sourced from ``parsed_data`` when available
    (``amount`` in major units rounded to cents, ``currency`` as-is).

    Raises :class:`PendingNotFoundError` if the pending row is gone
    (likely swept by TTL — caller should surface this to the user).
    """

    def _q() -> ExpenseCompleted:
        client = get_supabase()
        pend_resp = (
            client.table(_PENDING_TABLE)
            .select("*")
            .eq("id", str(pending_id))
            .limit(1)
            .execute()
        )
        pend_rows = pend_resp.data or []
        if not pend_rows:
            raise PendingNotFoundError(str(pending_id))
        pending = pend_rows[0]

        parsed = pending.get("parsed_data") or {}
        derived_cents = amount_cents
        if derived_cents is None and isinstance(parsed, dict):
            amt = parsed.get("amount")
            if isinstance(amt, int | float):
                derived_cents = int(round(float(amt) * 100))
        derived_currency = currency
        if derived_currency is None and isinstance(parsed, dict):
            cur = parsed.get("currency")
            if isinstance(cur, str):
                derived_currency = cur

        completed_payload: dict[str, Any] = {
            "splitwise_expense_id": splitwise_expense_id,
            "telegram_message_id": pending.get("telegram_message_id"),
            "telegram_group_id": pending.get("telegram_group_id"),
            "payer_telegram_user_id": pending.get("payer_telegram_user_id"),
            "amount_cents": derived_cents,
            "currency": derived_currency,
        }
        ins_resp = client.table(_COMPLETED_TABLE).insert(completed_payload).execute()
        ins_rows = ins_resp.data or []
        if not ins_rows:
            raise RuntimeError("mark_completed: insert returned no rows")
        completed = ExpenseCompleted.model_validate(ins_rows[0])

        client.table(_PENDING_TABLE).delete().eq("id", str(pending_id)).execute()
        return completed

    return await asyncio.to_thread(_q)


async def sweep_expired_pending() -> int:
    """Delete pending expenses whose ``expires_at`` is in the past.

    Returns the number of rows deleted. Brick F schedules this on a
    timer; this brick just provides the function.
    """

    def _q() -> int:
        client = get_supabase()
        now_iso = datetime.now(UTC).isoformat()
        resp = client.table(_PENDING_TABLE).delete().lt("expires_at", now_iso).execute()
        return len(resp.data or [])

    return await asyncio.to_thread(_q)


__all__ = [
    "PendingNotFoundError",
    "create_pending",
    "delete_pending",
    "get_pending",
    "mark_completed",
    "sweep_expired_pending",
]
