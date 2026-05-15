"""CRUD wrappers for the ``users`` table.

All functions wrap the sync ``supabase-py`` calls in ``asyncio.to_thread``
so callers can ``await`` them like any other I/O coroutine in this app.

Token encryption is *not* this module's concern — Brick C wraps these
to handle ``cryptography.fernet`` encrypt/decrypt. Here we only deal
with opaque strings.
"""

from __future__ import annotations

import asyncio

from app.db.client import get_supabase
from app.db.models import User

_TABLE = "users"


async def get_user(telegram_user_id: int) -> User | None:
    """Return the user row or ``None`` if no such user exists."""

    def _q() -> User | None:
        client = get_supabase()
        resp = (
            client.table(_TABLE)
            .select("*")
            .eq("telegram_user_id", telegram_user_id)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            return None
        return User.model_validate(rows[0])

    return await asyncio.to_thread(_q)


async def upsert_user(
    telegram_user_id: int,
    telegram_username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> User:
    """Insert or update a user row by ``telegram_user_id``.

    Only identity columns are touched here — Splitwise token fields are
    set separately via :func:`set_user_token` so we never accidentally
    clobber a stored token on a routine identity refresh.

    Fields passed as ``None`` are *omitted* from the payload (and thus
    not overwritten in an existing row). This means calling
    ``upsert_user(123, first_name="Prem")`` will set first_name without
    nuking any previously-stored telegram_username — important because
    Telegram updates may carry first_name without username, or vice
    versa.
    """

    def _q() -> User:
        client = get_supabase()
        payload: dict[str, object] = {"telegram_user_id": telegram_user_id}
        if telegram_username is not None:
            payload["telegram_username"] = telegram_username
        if first_name is not None:
            payload["first_name"] = first_name
        if last_name is not None:
            payload["last_name"] = last_name
        resp = (
            client.table(_TABLE)
            .upsert(payload, on_conflict="telegram_user_id")
            .execute()
        )
        rows = resp.data or []
        if not rows:
            raise RuntimeError("upsert_user: Supabase returned no rows")
        return User.model_validate(rows[0])

    return await asyncio.to_thread(_q)


async def set_user_token(
    telegram_user_id: int,
    encrypted_token: str,
    splitwise_user_id: int,
) -> None:
    """Persist an (already-encrypted) Splitwise token for the user.

    Uses upsert so OAuth-first users (who may not have a prior row) and
    re-auth flows both work. ``encrypted_token`` is stored verbatim;
    callers must encrypt before calling.
    """

    def _q() -> None:
        client = get_supabase()
        payload = {
            "telegram_user_id": telegram_user_id,
            "splitwise_access_token": encrypted_token,
            "splitwise_user_id": splitwise_user_id,
        }
        client.table(_TABLE).upsert(payload, on_conflict="telegram_user_id").execute()

    await asyncio.to_thread(_q)


__all__ = ["get_user", "set_user_token", "upsert_user"]
