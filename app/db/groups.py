"""CRUD wrappers for the ``groups`` and ``group_memberships`` tables.

``record_membership`` is the workhorse — Brick F calls it on every
inbound message to incrementally build the per-group roster (Telegram
gives us no way to list members otherwise).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.db.client import get_supabase
from app.db.models import Group, User

_GROUPS_TABLE = "groups"
_MEMBERSHIPS_TABLE = "group_memberships"
_USERS_TABLE = "users"


async def upsert_group(telegram_group_id: int, name: str | None) -> Group:
    """Insert or update a group row by ``telegram_group_id``."""

    def _q() -> Group:
        client = get_supabase()
        payload = {
            "telegram_group_id": telegram_group_id,
            "telegram_group_name": name,
        }
        resp = (
            client.table(_GROUPS_TABLE)
            .upsert(payload, on_conflict="telegram_group_id")
            .execute()
        )
        rows = resp.data or []
        if not rows:
            raise RuntimeError("upsert_group: Supabase returned no rows")
        return Group.model_validate(rows[0])

    return await asyncio.to_thread(_q)


async def record_membership(
    telegram_user_id: int,
    telegram_group_id: int,
) -> None:
    """Upsert a (user, group) pair and refresh ``last_seen_at``.

    Called on every message seen so the roster stays current.
    """

    def _q() -> None:
        client = get_supabase()
        payload = {
            "telegram_user_id": telegram_user_id,
            "telegram_group_id": telegram_group_id,
            "last_seen_at": datetime.now(UTC).isoformat(),
        }
        client.table(_MEMBERSHIPS_TABLE).upsert(
            payload,
            on_conflict="telegram_user_id,telegram_group_id",
        ).execute()

    await asyncio.to_thread(_q)


async def list_group_members(telegram_group_id: int) -> list[User]:
    """Return ``User`` rows for every member known to be in the group.

    Implemented as two queries (memberships -> user ids -> users) rather
    than a join so this works against vanilla PostgREST without
    requiring an explicit FK relationship to be declared in Supabase.
    """

    def _q() -> list[User]:
        client = get_supabase()
        m_resp = (
            client.table(_MEMBERSHIPS_TABLE)
            .select("telegram_user_id")
            .eq("telegram_group_id", telegram_group_id)
            .execute()
        )
        user_ids = [
            row["telegram_user_id"]
            for row in (m_resp.data or [])
            if row.get("telegram_user_id") is not None
        ]
        if not user_ids:
            return []
        u_resp = (
            client.table(_USERS_TABLE)
            .select("*")
            .in_("telegram_user_id", user_ids)
            .execute()
        )
        return [User.model_validate(row) for row in (u_resp.data or [])]

    return await asyncio.to_thread(_q)


__all__ = ["list_group_members", "record_membership", "upsert_group"]
