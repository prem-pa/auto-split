"""Tests for ``app.db.users``."""

from __future__ import annotations

import pytest

from app.db import users
from tests.db.conftest import FakeSupabaseClient


def _chain_methods(chain: list) -> list[str]:
    return [c.method for c in chain]


@pytest.mark.asyncio
async def test_get_user_returns_model_when_row_present(
    fake_supabase: FakeSupabaseClient,
) -> None:
    fake_supabase.queue([{"telegram_user_id": 42, "telegram_username": "prem"}])
    u = await users.get_user(42)
    assert u is not None
    assert u.telegram_user_id == 42
    assert u.telegram_username == "prem"

    # Verify the query chain: table → select → eq → limit → execute
    table_name, chain = fake_supabase.calls[0]
    assert table_name == "users"
    assert _chain_methods(chain) == ["select", "eq", "limit"]
    assert chain[1].args == ("telegram_user_id", 42)
    assert chain[2].args == (1,)


@pytest.mark.asyncio
async def test_get_user_returns_none_when_empty(
    fake_supabase: FakeSupabaseClient,
) -> None:
    fake_supabase.queue([])
    u = await users.get_user(42)
    assert u is None


@pytest.mark.asyncio
async def test_upsert_user_inserts_and_returns_row(
    fake_supabase: FakeSupabaseClient,
) -> None:
    fake_supabase.queue([{"telegram_user_id": 7, "telegram_username": "alice"}])
    u = await users.upsert_user(7, "alice")
    assert u.telegram_user_id == 7
    table, chain = fake_supabase.calls[0]
    assert table == "users"
    assert chain[0].method == "upsert"
    payload = chain[0].args[0]
    assert payload["telegram_user_id"] == 7
    assert payload["telegram_username"] == "alice"
    # We do NOT clobber the token on a routine upsert.
    assert "splitwise_access_token" not in payload
    # And we explicitly pin the conflict target.
    assert chain[0].kwargs == {"on_conflict": "telegram_user_id"}


@pytest.mark.asyncio
async def test_upsert_user_raises_when_supabase_returns_no_rows(
    fake_supabase: FakeSupabaseClient,
) -> None:
    fake_supabase.queue([])
    with pytest.raises(RuntimeError):
        await users.upsert_user(7, "alice")


@pytest.mark.asyncio
async def test_set_user_token_payload(fake_supabase: FakeSupabaseClient) -> None:
    fake_supabase.queue([{"telegram_user_id": 7}])  # not consumed by caller
    await users.set_user_token(
        telegram_user_id=7,
        encrypted_token="ENC::abc",
        splitwise_user_id=999,
    )
    table, chain = fake_supabase.calls[0]
    assert table == "users"
    assert chain[0].method == "upsert"
    payload = chain[0].args[0]
    assert payload == {
        "telegram_user_id": 7,
        "splitwise_access_token": "ENC::abc",
        "splitwise_user_id": 999,
    }
    assert chain[0].kwargs == {"on_conflict": "telegram_user_id"}
