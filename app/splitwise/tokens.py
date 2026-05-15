"""Symmetric encryption for Splitwise access tokens at rest.

We use Fernet (AES-128-CBC + HMAC-SHA256) keyed by
``settings.token_encryption_key``. The key must be a URL-safe base64-encoded
32-byte value — exactly the format ``cryptography.fernet.Fernet.generate_key()``
produces.

Generate one with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

and put it in ``.env`` as ``TOKEN_ENCRYPTION_KEY``.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet

from app.config import settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """Return a cached ``Fernet`` instance built from the configured key.

    Cached so we don't pay the key-parse cost on every call. The cache is
    invalidated implicitly when the process restarts; tests that swap the
    key must call ``_fernet.cache_clear()``.
    """
    key = settings.token_encryption_key.get_secret_value()
    if not key:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY is not set. Generate one with "
            "`python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'`."
        )
    # Fernet accepts ``str`` or ``bytes``; pass through as-is.
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    """Encrypt ``plaintext`` and return a URL-safe ciphertext string.

    Raises ``RuntimeError`` if the encryption key isn't configured.
    """
    token_bytes = _fernet().encrypt(plaintext.encode("utf-8"))
    return token_bytes.decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Decrypt ``ciphertext`` produced by :func:`encrypt`.

    Raises ``cryptography.fernet.InvalidToken`` on tampered / wrong-key
    input, or ``RuntimeError`` if the encryption key isn't configured.
    """
    plain_bytes = _fernet().decrypt(ciphertext.encode("ascii"))
    return plain_bytes.decode("utf-8")


__all__ = ["decrypt", "encrypt"]
