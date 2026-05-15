"""Encryption-aware adapters over ``app.db.users``.

The persistence layer (Brick E) deals in opaque strings. This module wraps
every read/write with Fernet encrypt/decrypt so plaintext tokens never
leave this package when they touch the database.

Brick F should always go through these adapters — not ``app.db.users``
directly — to avoid accidentally storing or reading raw tokens.
"""

from __future__ import annotations

from app.db.users import get_user, set_user_token
from app.splitwise.tokens import decrypt, encrypt


async def save_user_token(
    telegram_user_id: int,
    splitwise_user_id: int,
    plain_token: str,
) -> None:
    """Encrypt ``plain_token`` and persist it for ``telegram_user_id``.

    Idempotent: re-auth simply overwrites the previous ciphertext.
    """
    encrypted = encrypt(plain_token)
    await set_user_token(
        telegram_user_id=telegram_user_id,
        encrypted_token=encrypted,
        splitwise_user_id=splitwise_user_id,
    )


async def load_user_token(telegram_user_id: int) -> tuple[str, int] | None:
    """Return ``(plain_token, splitwise_user_id)`` for the user, or ``None``.

    ``None`` means either the user row is missing or they haven't completed
    OAuth yet (no encrypted token, no splitwise_user_id). Callers should
    treat ``None`` as "not connected" and prompt the user to /start.

    Raises ``cryptography.fernet.InvalidToken`` if the stored ciphertext
    fails to decrypt — that means key rotation broke an existing row, and
    we want it loud, not silent.
    """
    user = await get_user(telegram_user_id)
    if user is None:
        return None
    if not user.splitwise_access_token or user.splitwise_user_id is None:
        return None
    plain = decrypt(user.splitwise_access_token)
    return plain, user.splitwise_user_id


__all__ = ["load_user_token", "save_user_token"]
