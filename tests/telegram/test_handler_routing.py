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


# After Brick F replaced the Phase-1 echo stubs with real dispatch into
# ``app.services.expense_pipeline``, these tests now verify that the
# orchestrator entry points get called with the right Update — Brick F's
# own tests cover what the pipeline then does.


@pytest.fixture
def _stub_orchestrator(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the pipeline entry points with no-op recorders."""
    captured: dict[str, Any] = {"messages": [], "callbacks": []}

    async def fake_incoming(update: Any) -> None:
        captured["messages"].append(update)

    async def fake_callback(cq: Any) -> None:
        captured["callbacks"].append(cq)

    monkeypatch.setattr(
        "app.services.expense_pipeline.handle_incoming_message", fake_incoming
    )
    monkeypatch.setattr("app.services.expense_pipeline.handle_callback", fake_callback)
    monkeypatch.setattr(
        "app.telegram.handlers.expense_pipeline.handle_incoming_message",
        fake_incoming,
    )
    monkeypatch.setattr(
        "app.telegram.handlers.expense_pipeline.handle_callback", fake_callback
    )
    return captured


def test_text_message_routes_to_text_handler(
    client: TestClient,
    sent_messages: list[dict[str, Any]],
    _stub_orchestrator: dict[str, Any],
) -> None:
    resp = client.post("/telegram/webhook", json=_text_update("hi"), headers=_HEADERS)
    assert resp.status_code == 200
    # The orchestrator was invoked with the parsed Update.
    assert len(_stub_orchestrator["messages"]) == 1
    msg = _stub_orchestrator["messages"][0].message
    assert msg is not None and msg.text == "hi"


def test_photo_message_routes_to_photo_handler(
    client: TestClient,
    sent_messages: list[dict[str, Any]],
    _stub_orchestrator: dict[str, Any],
) -> None:
    resp = client.post("/telegram/webhook", json=_photo_update(), headers=_HEADERS)
    assert resp.status_code == 200
    assert len(_stub_orchestrator["messages"]) == 1
    msg = _stub_orchestrator["messages"][0].message
    assert msg is not None and msg.photo


def test_voice_message_routes_to_voice_handler(
    client: TestClient,
    sent_messages: list[dict[str, Any]],
    _stub_orchestrator: dict[str, Any],
) -> None:
    resp = client.post("/telegram/webhook", json=_voice_update(), headers=_HEADERS)
    assert resp.status_code == 200
    assert len(_stub_orchestrator["messages"]) == 1
    msg = _stub_orchestrator["messages"][0].message
    assert msg is not None and msg.voice is not None


def test_callback_query_routes_to_callback_handler(
    client: TestClient,
    sent_messages: list[dict[str, Any]],
    _mock_answer_callback_query: None,
    _stub_orchestrator: dict[str, Any],
) -> None:
    resp = client.post(
        "/telegram/webhook",
        json=_callback_update("cf:deadbeef"),
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    assert len(_stub_orchestrator["callbacks"]) == 1
    cq = _stub_orchestrator["callbacks"][0]
    assert cq.data == "cf:deadbeef"


def test_voice_takes_priority_over_text_when_both_present(
    client: TestClient,
    sent_messages: list[dict[str, Any]],
    _stub_orchestrator: dict[str, Any],
) -> None:
    """The router classifies voice messages before text — same Update
    still produces a single dispatch into the orchestrator."""
    update = _voice_update()
    resp = client.post("/telegram/webhook", json=update, headers=_HEADERS)
    assert resp.status_code == 200
    assert len(_stub_orchestrator["messages"]) == 1


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


# --- allowlist behaviour ---------------------------------------------------


def test_empty_allowlist_admits_anyone(
    client: TestClient,
    sent_messages: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    _stub_orchestrator: dict[str, Any],
) -> None:
    """Default config has an empty allowlist, so every user is allowed."""
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [])
    resp = client.post(
        "/telegram/webhook", json=_text_update("hi", chat_id=42), headers=_HEADERS
    )
    assert resp.status_code == 200
    # Handler ran → the orchestrator was invoked, no rejection message.
    assert sent_messages == []
    assert len(_stub_orchestrator["messages"]) == 1


def test_allowlisted_user_is_admitted(
    client: TestClient,
    sent_messages: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    _stub_orchestrator: dict[str, Any],
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [42])
    resp = client.post(
        "/telegram/webhook", json=_text_update("hello", chat_id=42), headers=_HEADERS
    )
    assert resp.status_code == 200
    assert sent_messages == []
    assert len(_stub_orchestrator["messages"]) == 1


def test_disallowed_user_gets_invite_only_reply_and_no_handler_run(
    client: TestClient,
    sent_messages: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    # 99 is allowed; 42 (our fixture sender) is not.
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [99])
    resp = client.post(
        "/telegram/webhook", json=_text_update("snoop", chat_id=42), headers=_HEADERS
    )
    assert resp.status_code == 200
    # Exactly one message: the rejection (no "got: snoop" echo).
    assert len(sent_messages) == 1
    rejection = sent_messages[0]
    assert rejection["chat_id"] == 42
    assert "invite-only" in rejection["text"].lower()
    # The rejection includes the user's ID so they can request access.
    assert "42" in rejection["text"]


def test_allowlist_applies_to_callback_queries_too(
    client: TestClient,
    sent_messages: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    _mock_answer_callback_query: None,
) -> None:
    """A non-allowlisted user tapping an inline keyboard gets rejected."""
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [99])
    resp = client.post(
        "/telegram/webhook", json=_callback_update("cf:deadbeef"), headers=_HEADERS
    )
    assert resp.status_code == 200
    assert len(sent_messages) == 1
    assert "invite-only" in sent_messages[0]["text"].lower()


# --- anonymous-sender reject path -----------------------------------------


def test_group_anonymous_admin_gets_friendly_explanation_not_invite_only(
    client: TestClient,
    sent_messages: list[dict[str, Any]],
) -> None:
    """Messages from @GroupAnonymousBot (id 1087968824) should be rejected
    with a specific explanation, not the generic invite-only message —
    the user can't change their ID and 'allowlist 1087968824' is wrong
    advice anyway (it'd let any anonymous admin through)."""
    payload = _text_update("yo")
    payload["message"]["from"]["id"] = 1087968824  # @GroupAnonymousBot
    resp = client.post("/telegram/webhook", json=payload, headers=_HEADERS)
    assert resp.status_code == 200
    assert len(sent_messages) == 1
    reply = sent_messages[0]["text"].lower()
    assert "anonymous" in reply
    # Crucially does NOT use the misleading "invite-only / allowlist your ID"
    # template (the user can't allowlist a Telegram-internal proxy id).
    assert "invite-only" not in reply
    assert "1087968824" not in reply


def test_channel_bot_proxy_also_rejected_with_friendly_message(
    client: TestClient,
    sent_messages: list[dict[str, Any]],
) -> None:
    """@Channel_Bot (id 136817688) — the proxy for channel-as-sender —
    gets the same friendly explanation."""
    payload = _text_update("yo")
    payload["message"]["from"]["id"] = 136817688  # @Channel_Bot
    resp = client.post("/telegram/webhook", json=payload, headers=_HEADERS)
    assert resp.status_code == 200
    assert len(sent_messages) == 1
    assert "anonymous" in sent_messages[0]["text"].lower()
