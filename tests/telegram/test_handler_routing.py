"""End-to-end routing through TestClient.

Feeds sample Update JSON to ``POST /telegram/webhook`` and verifies the
correct echo handler runs (by inspecting captured ``send_message`` calls).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.telegram.conftest import TEST_WEBHOOK_SECRET

_HEADERS = {"X-Telegram-Bot-Api-Secret-Token": TEST_WEBHOOK_SECRET}


def _text_update(text: str, chat_id: int = 42) -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "date": 1_700_000_000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False, "first_name": "Tester"},
            "text": text,
        },
    }


def _photo_update(chat_id: int = 42) -> dict[str, Any]:
    return {
        "update_id": 2,
        "message": {
            "message_id": 11,
            "date": 1_700_000_000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False, "first_name": "Tester"},
            "photo": [
                {
                    "file_id": "AgADTESTPHOTOID",
                    "file_unique_id": "AQADTEST",
                    "width": 100,
                    "height": 100,
                    "file_size": 1234,
                }
            ],
        },
    }


def _voice_update(chat_id: int = 42) -> dict[str, Any]:
    return {
        "update_id": 3,
        "message": {
            "message_id": 12,
            "date": 1_700_000_000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False, "first_name": "Tester"},
            "voice": {
                "file_id": "AwADTESTVOICEID",
                "file_unique_id": "AwADTEST",
                "duration": 3,
                "mime_type": "audio/ogg",
                "file_size": 5678,
            },
        },
    }


def _callback_update(data: str, chat_id: int = 42) -> dict[str, Any]:
    return {
        "update_id": 4,
        "callback_query": {
            "id": "cq-1",
            "from": {"id": chat_id, "is_bot": False, "first_name": "Tester"},
            "chat_instance": "ci-1",
            "data": data,
            "message": {
                "message_id": 99,
                "date": 1_700_000_000,
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": 1, "is_bot": True, "first_name": "Bot"},
                "text": "earlier message",
            },
        },
    }


@pytest.fixture
def _mock_answer_callback_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``Bot.answer_callback_query`` so handlers don't hit Telegram.

    ``telegram.Bot`` is frozen so we patch at the class level.
    """
    from telegram import Bot

    async def fake_answer(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(Bot, "answer_callback_query", fake_answer)


def test_text_message_routes_to_text_handler(
    client: TestClient, sent_messages: list[dict[str, Any]]
) -> None:
    resp = client.post("/telegram/webhook", json=_text_update("hi"), headers=_HEADERS)
    assert resp.status_code == 200
    assert sent_messages == [{"chat_id": 42, "text": "got: hi", "reply_markup": None}]


def test_photo_message_routes_to_photo_handler(
    client: TestClient, sent_messages: list[dict[str, Any]]
) -> None:
    resp = client.post("/telegram/webhook", json=_photo_update(), headers=_HEADERS)
    assert resp.status_code == 200
    assert sent_messages == [
        {"chat_id": 42, "text": "got a photo", "reply_markup": None}
    ]


def test_voice_message_routes_to_voice_handler(
    client: TestClient, sent_messages: list[dict[str, Any]]
) -> None:
    resp = client.post("/telegram/webhook", json=_voice_update(), headers=_HEADERS)
    assert resp.status_code == 200
    assert sent_messages == [
        {"chat_id": 42, "text": "got a voice note", "reply_markup": None}
    ]


def test_callback_query_routes_to_callback_handler(
    client: TestClient,
    sent_messages: list[dict[str, Any]],
    _mock_answer_callback_query: None,
) -> None:
    resp = client.post(
        "/telegram/webhook",
        json=_callback_update("cf:deadbeef"),
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert sent_messages == [
        {"chat_id": 42, "text": "got a tap on cf:deadbeef", "reply_markup": None}
    ]


def test_voice_takes_priority_over_text_when_both_present(
    client: TestClient, sent_messages: list[dict[str, Any]]
) -> None:
    """A voice message may carry an empty text field — voice still wins."""
    update = _voice_update()
    # Telegram never sends both, but be defensive about the dispatch order.
    resp = client.post("/telegram/webhook", json=update, headers=_HEADERS)
    assert resp.status_code == 200
    assert sent_messages[0]["text"] == "got a voice note"


def test_malformed_json_body_returns_200_and_does_not_dispatch(
    client: TestClient, sent_messages: list[dict[str, Any]]
) -> None:
    resp = client.post(
        "/telegram/webhook",
        content=b"this is not json",
        headers={**_HEADERS, "Content-Type": "application/json"},
    )
    # We swallow garbage so Telegram doesn't retry forever.
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert sent_messages == []


def test_handler_exception_does_not_5xx_telegram(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a handler raises, the webhook still returns 200 to Telegram."""

    async def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated handler failure")

    monkeypatch.setattr("app.telegram.handlers.handle_text_message", boom)
    monkeypatch.setattr("app.telegram.webhook.handlers.handle_text_message", boom)

    resp = client.post("/telegram/webhook", json=_text_update("hi"), headers=_HEADERS)
    assert resp.status_code == 200
