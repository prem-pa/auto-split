"""Signed-state round-trip + expiry / tamper rejection."""

from __future__ import annotations

import time

import pytest

from app.splitwise import state as state_mod
from app.splitwise.state import InvalidState, mint_state, verify_state


def test_mint_verify_roundtrip() -> None:
    token = mint_state(123456789)
    assert verify_state(token) == 123456789


def test_state_is_opaque() -> None:
    token = mint_state(42)
    # Fernet tokens always start with "gAAAAA" (version byte 0x80, then
    # the timestamp and IV in URL-safe base64). Proves we returned an
    # encrypted token, not the plaintext payload.
    assert token.startswith("gAAAAA")
    # No plaintext claim keys leaked.
    assert "telegram_user_id" not in token
    assert '"u":' not in token  # JSON key for user id inside the payload
    assert '"exp":' not in token


def test_tampered_state_rejected() -> None:
    token = mint_state(1)
    tampered = token[:-2] + "AA"
    with pytest.raises(InvalidState):
        verify_state(tampered)


def test_empty_state_rejected() -> None:
    with pytest.raises(InvalidState):
        verify_state("")


def test_garbage_state_rejected() -> None:
    with pytest.raises(InvalidState):
        verify_state("not-a-real-state-token")


def test_expired_state_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fast-forward time past the 10-minute TTL and confirm rejection."""
    token = mint_state(7)
    now = time.time()
    # Bump the clock by 11 minutes for the verify path only.
    monkeypatch.setattr(state_mod.time, "time", lambda: now + 11 * 60)
    with pytest.raises(InvalidState):
        verify_state(token)
