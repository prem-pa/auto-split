"""Tests for ``app.db.expenses``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.db import expenses
from tests.db.conftest import FakeSupabaseClient


@pytest.mark.asyncio
async def test_create_pending_inserts_payload(
    fake_supabase: FakeSupabaseClient,
) -> None:
    pid = uuid4()
    fake_supabase.queue(
        [
            {
                "id": str(pid),
                "telegram_message_id": 42,
                "telegram_group_id": -100,
                "payer_telegram_user_id": 1,
                "parsed_data": {"amount": 47.32, "currency": "USD"},
                "receipt_image_url": None,
                "expires_at": "2026-05-15T00:10:00+00:00",
            }
        ]
    )
    pending = await expenses.create_pending(
        telegram_message_id=42,
        telegram_group_id=-100,
        payer_telegram_user_id=1,
        parsed_data={"amount": 47.32, "currency": "USD"},
    )
    assert pending.id == pid
    assert pending.parsed_data == {"amount": 47.32, "currency": "USD"}

    table, chain = fake_supabase.calls[0]
    assert table == "expenses_pending"
    assert chain[0].method == "insert"
    payload = chain[0].args[0]
    assert payload["telegram_message_id"] == 42
    assert payload["parsed_data"] == {"amount": 47.32, "currency": "USD"}
    # expires_at omitted → DB default applies.
    assert "expires_at" not in payload


@pytest.mark.asyncio
async def test_create_pending_respects_explicit_expires_at(
    fake_supabase: FakeSupabaseClient,
) -> None:
    expires = datetime.now(UTC) + timedelta(minutes=5)
    pid = uuid4()
    fake_supabase.queue(
        [
            {
                "id": str(pid),
                "parsed_data": {},
                "expires_at": expires.isoformat(),
            }
        ]
    )
    await expenses.create_pending(
        telegram_message_id=1,
        telegram_group_id=2,
        payer_telegram_user_id=3,
        parsed_data={},
        expires_at=expires,
    )
    _, chain = fake_supabase.calls[0]
    payload = chain[0].args[0]
    assert payload["expires_at"] == expires.isoformat()


@pytest.mark.asyncio
async def test_get_pending_returns_none_when_missing(
    fake_supabase: FakeSupabaseClient,
) -> None:
    fake_supabase.queue([])
    result = await expenses.get_pending(uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_get_pending_chain_uses_eq_id(
    fake_supabase: FakeSupabaseClient,
) -> None:
    pid = uuid4()
    fake_supabase.queue(
        [
            {
                "id": str(pid),
                "parsed_data": {},
                "expires_at": "2026-05-15T00:00:00+00:00",
            }
        ]
    )
    result = await expenses.get_pending(pid)
    assert result is not None and result.id == pid
    table, chain = fake_supabase.calls[0]
    assert table == "expenses_pending"
    assert [c.method for c in chain] == ["select", "eq", "limit"]
    assert chain[1].args == ("id", str(pid))


@pytest.mark.asyncio
async def test_delete_pending_chain(fake_supabase: FakeSupabaseClient) -> None:
    pid = uuid4()
    fake_supabase.queue([])
    await expenses.delete_pending(pid)
    table, chain = fake_supabase.calls[0]
    assert table == "expenses_pending"
    assert chain[0].method == "delete"
    assert chain[1].method == "eq"
    assert chain[1].args == ("id", str(pid))


@pytest.mark.asyncio
async def test_mark_completed_moves_row_and_derives_amount(
    fake_supabase: FakeSupabaseClient,
) -> None:
    pid = uuid4()
    cid = uuid4()
    # 1. fetch pending row
    fake_supabase.queue(
        [
            {
                "id": str(pid),
                "telegram_message_id": 42,
                "telegram_group_id": -100,
                "payer_telegram_user_id": 1,
                "parsed_data": {"amount": 47.32, "currency": "USD"},
                "receipt_image_url": None,
                "expires_at": "2026-05-15T00:10:00+00:00",
            }
        ]
    )
    # 2. insert completed row
    fake_supabase.queue(
        [
            {
                "id": str(cid),
                "splitwise_expense_id": 999,
                "telegram_message_id": 42,
                "telegram_group_id": -100,
                "payer_telegram_user_id": 1,
                "amount_cents": 4732,
                "currency": "USD",
                "created_at": "2026-05-15T00:00:00+00:00",
            }
        ]
    )
    # 3. delete pending row
    fake_supabase.queue([])

    completed = await expenses.mark_completed(pid, splitwise_expense_id=999)
    assert completed.id == cid
    assert completed.amount_cents == 4732
    assert completed.currency == "USD"

    # Step 1 selects pending row.
    assert fake_supabase.calls[0][0] == "expenses_pending"
    # Step 2 inserts into completed with derived amount/currency.
    table_ins, chain_ins = fake_supabase.calls[1]
    assert table_ins == "expenses_completed"
    ins_payload = chain_ins[0].args[0]
    assert ins_payload["splitwise_expense_id"] == 999
    assert ins_payload["amount_cents"] == 4732
    assert ins_payload["currency"] == "USD"
    # Step 3 deletes pending.
    table_del, chain_del = fake_supabase.calls[2]
    assert table_del == "expenses_pending"
    assert chain_del[0].method == "delete"


@pytest.mark.asyncio
async def test_mark_completed_raises_when_pending_gone(
    fake_supabase: FakeSupabaseClient,
) -> None:
    fake_supabase.queue([])  # pending lookup empty
    with pytest.raises(expenses.PendingNotFoundError):
        await expenses.mark_completed(uuid4(), splitwise_expense_id=1)


@pytest.mark.asyncio
async def test_mark_completed_respects_explicit_overrides(
    fake_supabase: FakeSupabaseClient,
) -> None:
    pid = uuid4()
    cid = uuid4()
    fake_supabase.queue(
        [
            {
                "id": str(pid),
                "telegram_message_id": 1,
                "telegram_group_id": 2,
                "payer_telegram_user_id": 3,
                "parsed_data": {"amount": 10.00, "currency": "USD"},
                "expires_at": "2026-05-15T00:10:00+00:00",
            }
        ]
    )
    fake_supabase.queue(
        [
            {
                "id": str(cid),
                "splitwise_expense_id": 1,
                "amount_cents": 9999,
                "currency": "EUR",
            }
        ]
    )
    fake_supabase.queue([])
    completed = await expenses.mark_completed(
        pid, splitwise_expense_id=1, amount_cents=9999, currency="EUR"
    )
    assert completed.amount_cents == 9999
    _, chain = fake_supabase.calls[1]
    payload = chain[0].args[0]
    # Overrides win over parsed_data.
    assert payload["amount_cents"] == 9999
    assert payload["currency"] == "EUR"


@pytest.mark.asyncio
async def test_sweep_expired_pending_returns_count(
    fake_supabase: FakeSupabaseClient,
) -> None:
    fake_supabase.queue(
        [{"id": str(uuid4())}, {"id": str(uuid4())}, {"id": str(uuid4())}]
    )
    n = await expenses.sweep_expired_pending()
    assert n == 3
    table, chain = fake_supabase.calls[0]
    assert table == "expenses_pending"
    assert chain[0].method == "delete"
    # The filter must be ``expires_at < now``.
    assert chain[1].method == "lt"
    assert chain[1].args[0] == "expires_at"


@pytest.mark.asyncio
async def test_sweep_expired_pending_zero_when_none(
    fake_supabase: FakeSupabaseClient,
) -> None:
    fake_supabase.queue([])
    n = await expenses.sweep_expired_pending()
    assert n == 0
