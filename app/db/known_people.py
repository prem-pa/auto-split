"""Persistence for cached Splitwise "known people".

Each bot user has a set of people they've split with on Splitwise (their
Splitwise friends). We cache that here so the orchestrator can resolve a name
like "split with Cody" against someone who has a Splitwise account but never
connected to the bot — they have a ``splitwise_user_id``, which is all
``create_expense`` needs. Synced from ``SplitwiseClient.get_friends`` via
:mod:`app.services.known_people_sync`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from app.db.client import get_supabase

_TABLE = "known_people"


class KnownPerson(BaseModel):
    """A Splitwise person in some bot user's known set.

    Has a ``splitwise_user_id`` but (deliberately) no ``telegram_user_id`` —
    these people need not be bot users.
    """

    model_config = ConfigDict(frozen=True)

    splitwise_user_id: int
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None


async def upsert_known_people(
    owner_telegram_user_id: int, people: list[KnownPerson]
) -> int:
    """Upsert ``people`` as ``owner``'s known set. Returns rows written.

    Note: this adds/refreshes; it does not prune people who are no longer
    Splitwise friends. Stale extras are harmless for name resolution.
    """
    if not people:
        return 0

    def _q() -> int:
        client = get_supabase()
        now_iso = datetime.now(UTC).isoformat()
        rows = [
            {
                "owner_telegram_user_id": owner_telegram_user_id,
                "splitwise_user_id": p.splitwise_user_id,
                "first_name": p.first_name,
                "last_name": p.last_name,
                "email": p.email,
                "source": "friend",
                "last_synced_at": now_iso,
            }
            for p in people
        ]
        resp = (
            client.table(_TABLE)
            .upsert(rows, on_conflict="owner_telegram_user_id,splitwise_user_id")
            .execute()
        )
        return len(resp.data or [])

    return await asyncio.to_thread(_q)


async def list_known_people(owner_telegram_user_id: int) -> list[KnownPerson]:
    """Return ``owner``'s cached known people (empty list if none)."""

    def _q() -> list[KnownPerson]:
        client = get_supabase()
        resp = (
            client.table(_TABLE)
            .select("splitwise_user_id, first_name, last_name, email")
            .eq("owner_telegram_user_id", owner_telegram_user_id)
            .execute()
        )
        return [KnownPerson.model_validate(row) for row in (resp.data or [])]

    return await asyncio.to_thread(_q)


__all__ = ["KnownPerson", "list_known_people", "upsert_known_people"]
