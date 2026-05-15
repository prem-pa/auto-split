"""Tests for ``app.db.groups``."""

from __future__ import annotations

import pytest

from app.db import groups
from tests.db.conftest import FakeSupabaseClient


@pytest.mark.asyncio
async def test_upsert_group_payload(fake_supabase: FakeSupabaseClient) -> None:
    fake_supabase.queue([{"telegram_group_id": -100, "telegram_group_name": "Roomies"}])
    g = await groups.upsert_group(-100, "Roomies")
    assert g.telegram_group_id == -100
    assert g.telegram_group_name == "Roomies"

    table, chain = fake_supabase.calls[0]
    assert table == "groups"
    assert chain[0].method == "upsert"
    payload = chain[0].args[0]
    assert payload == {
        "telegram_group_id": -100,
        "telegram_group_name": "Roomies",
    }
    assert chain[0].kwargs == {"on_conflict": "telegram_group_id"}


@pytest.mark.asyncio
async def test_record_membership_upserts_with_composite_conflict(
    fake_supabase: FakeSupabaseClient,
) -> None:
    fake_supabase.queue([{"telegram_user_id": 1, "telegram_group_id": -100}])
    await groups.record_membership(1, -100)
    table, chain = fake_supabase.calls[0]
    assert table == "group_memberships"
    assert chain[0].method == "upsert"
    payload = chain[0].args[0]
    assert payload["telegram_user_id"] == 1
    assert payload["telegram_group_id"] == -100
    assert "last_seen_at" in payload  # ISO timestamp string
    assert chain[0].kwargs == {"on_conflict": "telegram_user_id,telegram_group_id"}


@pytest.mark.asyncio
async def test_list_group_members_returns_empty_when_no_memberships(
    fake_supabase: FakeSupabaseClient,
) -> None:
    fake_supabase.queue([])  # memberships query returns nothing
    members = await groups.list_group_members(-100)
    assert members == []
    # Only the memberships query should have run.
    assert len(fake_supabase.calls) == 1
    assert fake_supabase.calls[0][0] == "group_memberships"


@pytest.mark.asyncio
async def test_list_group_members_fans_out_to_users(
    fake_supabase: FakeSupabaseClient,
) -> None:
    # First call: memberships lookup.
    fake_supabase.queue([{"telegram_user_id": 1}, {"telegram_user_id": 2}])
    # Second call: users lookup.
    fake_supabase.queue(
        [
            {"telegram_user_id": 1, "telegram_username": "alice"},
            {"telegram_user_id": 2, "telegram_username": "bob"},
        ]
    )
    members = await groups.list_group_members(-100)
    assert [m.telegram_user_id for m in members] == [1, 2]
    assert [m.telegram_username for m in members] == ["alice", "bob"]

    # Verify both query chains.
    t1, c1 = fake_supabase.calls[0]
    t2, c2 = fake_supabase.calls[1]
    assert t1 == "group_memberships"
    assert c1[1].args == ("telegram_group_id", -100)
    assert t2 == "users"
    # in_() with the deduped user-id list
    assert c2[1].method == "in_"
    assert c2[1].args[0] == "telegram_user_id"
    assert sorted(c2[1].args[1]) == [1, 2]
