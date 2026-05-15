"""Fernet encrypt/decrypt round-trip + tamper resistance."""

from __future__ import annotations

import pytest
from cryptography.fernet import InvalidToken

from app.splitwise.tokens import decrypt, encrypt


def test_encrypt_decrypt_roundtrip() -> None:
    plain = "splitwise-access-token-abc123"
    cipher = encrypt(plain)
    assert cipher != plain
    assert decrypt(cipher) == plain


def test_encrypt_is_non_deterministic() -> None:
    # Fernet bakes a random IV in, so identical plaintexts must produce
    # different ciphertexts. Sanity check we aren't using ECB or similar.
    a = encrypt("same")
    b = encrypt("same")
    assert a != b
    assert decrypt(a) == decrypt(b) == "same"


def test_decrypt_tampered_ciphertext_raises() -> None:
    cipher = encrypt("hello")
    # Flip a character somewhere in the middle to break the HMAC.
    tampered = cipher[: len(cipher) // 2] + "X" + cipher[len(cipher) // 2 + 1 :]
    with pytest.raises(InvalidToken):
        decrypt(tampered)


def test_decrypt_garbage_raises() -> None:
    with pytest.raises(InvalidToken):
        decrypt("not-a-real-fernet-token")


def test_encrypt_requires_configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the key is empty, encrypt() should raise loudly, not silently encrypt."""
    from pydantic import SecretStr

    from app.config import settings
    from app.splitwise.tokens import _fernet

    monkeypatch.setattr(settings, "token_encryption_key", SecretStr(""))
    _fernet.cache_clear()
    with pytest.raises(RuntimeError, match="TOKEN_ENCRYPTION_KEY"):
        encrypt("anything")
