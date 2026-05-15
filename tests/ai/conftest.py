"""Shared fixtures for Brick D (AI parsing) tests.

Goals:
    * Never hit Groq or Gemini for real.
    * Provide a canned ``ParsedExpense`` JSON blob we can reuse across
      tests.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from pydantic import SecretStr

from app.ai.provider import GroupContext

# Tiny 1×1 JPEG used in tests that need real-looking image bytes.
_JPEG_MAGIC = bytes.fromhex("ffd8ffe000104a46494600010100000100010000")
_JPEG_BODY = b"\x00" * 32 + b"\xff\xd9"
FAKE_JPEG_BYTES = _JPEG_MAGIC + _JPEG_BODY

FAKE_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force fake API keys onto the global settings singleton.

    Tests construct fake clients explicitly; this is just so the
    "settings has a value" guards don't trip during import / factory
    calls.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "groq_api_key", SecretStr("test-groq-key"))
    monkeypatch.setattr(settings, "gemini_api_key", SecretStr("test-gemini-key"))


@pytest.fixture(autouse=True)
def _reset_groq_client_cache() -> Iterator[None]:
    """Clear the LRU-cached Groq client between tests."""
    from app.ai.transcribe import _default_client

    _default_client.cache_clear()
    yield
    _default_client.cache_clear()


@pytest.fixture
def canned_expense_json() -> str:
    """A valid ``ParsedExpense`` JSON payload."""
    return json.dumps(
        {
            "amount": "47.32",
            "currency": "USD",
            "merchant": "Trader Joe's",
            "split_type": "equal",
            "splits": [
                {"name": "self", "share": 0.5},
                {"name": "Priya", "share": 0.5},
            ],
            "confidence": 0.85,
        }
    )


@pytest.fixture
def group_context() -> GroupContext:
    return GroupContext(
        payer_name="Prem",
        member_names=["Priya", "Cody"],
        default_currency="USD",
    )
