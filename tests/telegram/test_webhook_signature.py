"""Webhook signature validation."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.telegram.conftest import TEST_WEBHOOK_SECRET

# A minimal valid Update payload (text message). The webhook should never
# look at it when the signature check fails, but we still send something
# plausible so a 200 response is unambiguous.
_VALID_TEXT_UPDATE: dict[str, Any] = {
    "update_id": 1,
    "message": {
        "message_id": 10,
        "date": 1_700_000_000,
        "chat": {"id": 42, "type": "private"},
        "from": {"id": 42, "is_bot": False, "first_name": "Tester"},
        "text": "hi",
    },
}


def test_correct_secret_returns_200(
    client: TestClient, sent_messages: list[Any]
) -> None:
    resp = client.post(
        "/telegram/webhook",
        json=_VALID_TEXT_UPDATE,
        headers={"X-Telegram-Bot-Api-Secret-Token": TEST_WEBHOOK_SECRET},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    # And the handler ran — confirms the route actually dispatched.
    assert len(sent_messages) == 1


def test_wrong_secret_returns_403(client: TestClient, sent_messages: list[Any]) -> None:
    resp = client.post(
        "/telegram/webhook",
        json=_VALID_TEXT_UPDATE,
        headers={"X-Telegram-Bot-Api-Secret-Token": "definitely-not-the-secret"},
    )
    assert resp.status_code == 403
    assert sent_messages == []  # body must not be processed


def test_missing_secret_returns_403(
    client: TestClient, sent_messages: list[Any]
) -> None:
    resp = client.post("/telegram/webhook", json=_VALID_TEXT_UPDATE)
    assert resp.status_code == 403
    assert sent_messages == []


def test_unconfigured_secret_rejects_even_empty_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the server secret is unset, no request can authenticate."""
    from app.config import settings
    from app.main import create_app

    monkeypatch.setattr(settings, "telegram_webhook_secret", SecretStr(""))
    with TestClient(create_app()) as c:
        resp = c.post(
            "/telegram/webhook",
            json=_VALID_TEXT_UPDATE,
            headers={"X-Telegram-Bot-Api-Secret-Token": ""},
        )
    assert resp.status_code == 403
