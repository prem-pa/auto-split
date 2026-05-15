"""Storage adapters encrypt on save and decrypt on load."""

from __future__ import annotations

import pytest

from app.db.models import User
from app.splitwise.storage import load_user_token, save_user_token
from app.splitwise.tokens import decrypt


@pytest.mark.asyncio
async def test_save_user_token_encrypts_before_persisting(
    patch_db_users: dict,
) -> None:
    await save_user_token(
        telegram_user_id=111,
        splitwise_user_id=222,
        plain_token="raw-token-do-not-store-as-is",
    )
    assert len(patch_db_users["saved"]) == 1
    (tid, ciphertext, swid) = patch_db_users["saved"][0]
    assert tid == 111
    assert swid == 222
    # The persisted blob must NOT equal the plaintext.
    assert ciphertext != "raw-token-do-not-store-as-is"
    # And it must decrypt back cleanly.
    assert decrypt(ciphertext) == "raw-token-do-not-store-as-is"


@pytest.mark.asyncio
async def test_load_user_token_decrypts(patch_db_users: dict) -> None:
    # First save -> populates the in-memory user with a real ciphertext.
    await save_user_token(
        telegram_user_id=42, splitwise_user_id=99, plain_token="hello-token"
    )
    result = await load_user_token(42)
    assert result == ("hello-token", 99)


@pytest.mark.asyncio
async def test_load_user_token_missing_user(patch_db_users: dict) -> None:
    assert await load_user_token(404404) is None


@pytest.mark.asyncio
async def test_load_user_token_user_without_oauth(patch_db_users: dict) -> None:
    # User row exists (Telegram-only) but never completed OAuth.
    patch_db_users["users"][7] = User(telegram_user_id=7, telegram_username="bob")
    assert await load_user_token(7) is None
