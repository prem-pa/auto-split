"""Shared fixtures for Brick B tests.

Goals:
    * Never hit the real Telegram API.
    * Pin a known webhook secret so signature tests are deterministic.
    * Build a FastAPI ``TestClient`` from a fresh app instance using
      ``create_app()`` (proves our router is wired through the factory).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

# Test constants — DO NOT match anything real.
TEST_BOT_TOKEN = "123456:TEST-TOKEN-NOT-REAL"
TEST_WEBHOOK_SECRET = "test-webhook-secret-do-not-use"


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force test-only secrets onto the global ``settings`` singleton."""
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr(TEST_BOT_TOKEN))
    monkeypatch.setattr(
        settings, "telegram_webhook_secret", SecretStr(TEST_WEBHOOK_SECRET)
    )


@pytest.fixture(autouse=True)
def _reset_bot_singleton() -> Iterator[None]:
    """Clear the cached ``Bot`` between tests so settings overrides take effect."""
    from app.telegram.bot import get_bot

    get_bot.cache_clear()
    yield
    get_bot.cache_clear()


@pytest.fixture
def sent_messages(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture all ``send_message`` calls instead of hitting Telegram.

    Returns the shared list that handler code appends to. Tests assert on it.
    """
    sent: list[dict[str, Any]] = []

    async def fake_send_message(
        chat_id: int, text: str, reply_markup: Any | None = None
    ) -> dict[str, Any]:
        record = {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        sent.append(record)
        return record  # handlers don't use the return value in Phase 1

    # Patch all import sites: the bot module *and* the re-exports / handler use.
    monkeypatch.setattr("app.telegram.bot.send_message", fake_send_message)
    monkeypatch.setattr("app.telegram.handlers.send_message", fake_send_message)
    monkeypatch.setattr("app.telegram.webhook.send_message", fake_send_message)
    return sent


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A ``TestClient`` built from a fresh ``create_app()`` invocation."""
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c
