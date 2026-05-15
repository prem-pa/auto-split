"""Pydantic round-trip tests for the persistence models.

Two shapes matter:
    * "DB shape" — what ``supabase-py`` returns: dicts with ISO-8601
      timestamp strings and UUID strings.
    * "Typed shape" — what callers see: ``datetime`` and ``uuid.UUID``
      objects.

We test that ``model_validate`` accepts the DB shape and that
``model_dump(mode='json')`` produces a serializable payload.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.db.models import (
    ExpenseCompleted,
    ExpensePending,
    Group,
    GroupMembership,
    User,
)


def test_user_roundtrip_db_shape() -> None:
    row = {
        "telegram_user_id": 12345,
        "telegram_username": "prem",
        "splitwise_user_id": 7777,
        "splitwise_access_token": "ciphertext-blob",
        "default_currency": "USD",
        "created_at": "2026-05-15T12:34:56.000Z",
    }
    u = User.model_validate(row)
    assert u.telegram_user_id == 12345
    assert u.telegram_username == "prem"
    assert isinstance(u.created_at, datetime)
    dumped = u.model_dump(mode="json")
    assert dumped["telegram_user_id"] == 12345
    assert dumped["splitwise_access_token"] == "ciphertext-blob"


def test_user_defaults_when_columns_missing() -> None:
    # The minimum row Supabase can return is just the PK.
    u = User.model_validate({"telegram_user_id": 1})
    assert u.telegram_username is None
    assert u.splitwise_user_id is None
    assert u.default_currency == "USD"
    assert u.created_at is None


def test_user_extra_columns_ignored() -> None:
    u = User.model_validate({"telegram_user_id": 1, "some_future_column": "hello"})
    assert u.telegram_user_id == 1


def test_group_roundtrip() -> None:
    row = {
        "telegram_group_id": -1001234567890,
        "telegram_group_name": "Roomies",
        "splitwise_group_id": None,
        "created_at": "2026-05-15T00:00:00+00:00",
    }
    g = Group.model_validate(row)
    assert g.telegram_group_id == -1001234567890
    assert g.telegram_group_name == "Roomies"
    assert g.splitwise_group_id is None


def test_group_membership_roundtrip() -> None:
    row = {
        "telegram_user_id": 1,
        "telegram_group_id": 2,
        "last_seen_at": "2026-05-15T01:02:03+00:00",
    }
    m = GroupMembership.model_validate(row)
    assert m.telegram_user_id == 1
    assert m.telegram_group_id == 2
    assert isinstance(m.last_seen_at, datetime)


def test_expense_pending_uuid_string_parses() -> None:
    pid = uuid4()
    row = {
        "id": str(pid),
        "telegram_message_id": 42,
        "telegram_group_id": -100,
        "payer_telegram_user_id": 1,
        "parsed_data": {"amount": 47.32, "currency": "USD"},
        "receipt_image_url": None,
        "expires_at": "2026-05-15T00:10:00+00:00",
    }
    e = ExpensePending.model_validate(row)
    assert e.id == pid
    assert e.parsed_data == {"amount": 47.32, "currency": "USD"}
    assert isinstance(e.expires_at, datetime)


def test_expense_pending_from_typed_shape() -> None:
    pid = uuid4()
    now = datetime.now(UTC)
    e = ExpensePending(
        id=pid,
        telegram_message_id=1,
        telegram_group_id=2,
        payer_telegram_user_id=3,
        parsed_data={"x": 1},
        receipt_image_url=None,
        expires_at=now,
    )
    dumped = e.model_dump(mode="json")
    assert dumped["id"] == str(pid)
    assert isinstance(dumped["expires_at"], str)


def test_expense_completed_roundtrip() -> None:
    cid = uuid4()
    row = {
        "id": str(cid),
        "splitwise_expense_id": 999,
        "telegram_message_id": 42,
        "telegram_group_id": -100,
        "payer_telegram_user_id": 1,
        "amount_cents": 4732,
        "currency": "USD",
        "created_at": "2026-05-15T00:00:00+00:00",
    }
    c = ExpenseCompleted.model_validate(row)
    assert c.id == cid
    assert c.amount_cents == 4732
    assert c.currency == "USD"
