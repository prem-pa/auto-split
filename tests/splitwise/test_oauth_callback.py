"""``GET /oauth/callback`` happy path + rejection cases."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.splitwise.state import mint_state
from app.splitwise.tokens import decrypt

from .conftest import FakeSDKUser, FakeSplitwiseSDK


def test_route_is_registered() -> None:
    """The callback route must actually be wired into the app factory."""
    from app.main import create_app

    app = create_app()
    paths = {r.path for r in app.routes}
    assert "/oauth/callback" in paths


def test_callback_happy_path(
    client: TestClient,
    fake_sdk: type[FakeSplitwiseSDK],
    patch_db_users: dict[str, Any],
) -> None:
    # Pre-configure the (future) SDK instance the callback will build.
    # We can't pre-build it — the callback constructs Splitwise() itself —
    # so monkeypatch the *default* values on the class for this test.
    # Simpler: pre-seed an instance and rely on it being the next-created.
    # Even simpler: ``FakeSplitwiseSDK`` defaults already produce a token + user.

    state = mint_state(7777)
    resp = client.get("/oauth/callback", params={"code": "abc123", "state": state})

    assert resp.status_code == 200
    assert "Splitwise connected" in resp.text

    # Exactly one save happened, with the expected telegram_user_id and the
    # current-user id from the fake SDK (default 99). Token was encrypted.
    assert len(patch_db_users["saved"]) == 1
    tid, ciphertext, swid = patch_db_users["saved"][0]
    assert tid == 7777
    assert swid == 99  # FakeSplitwiseSDK default current_user id
    assert ciphertext != "swt-fake"
    assert decrypt(ciphertext) == "swt-fake"


def test_callback_rejects_missing_state(
    client: TestClient, fake_sdk: type[FakeSplitwiseSDK]
) -> None:
    resp = client.get("/oauth/callback", params={"code": "abc"})
    assert resp.status_code == 400


def test_callback_rejects_missing_code(
    client: TestClient, fake_sdk: type[FakeSplitwiseSDK]
) -> None:
    state = mint_state(1)
    resp = client.get("/oauth/callback", params={"state": state})
    assert resp.status_code == 400


def test_callback_rejects_bad_state(
    client: TestClient, fake_sdk: type[FakeSplitwiseSDK]
) -> None:
    resp = client.get(
        "/oauth/callback", params={"code": "abc", "state": "tampered-not-real"}
    )
    assert resp.status_code == 400


def test_callback_with_splitwise_error_param(
    client: TestClient, fake_sdk: type[FakeSplitwiseSDK]
) -> None:
    # Splitwise can redirect back with ?error=access_denied if the user
    # bailed out — render the error page, don't 500.
    resp = client.get("/oauth/callback", params={"error": "access_denied"})
    assert resp.status_code == 400
    assert "Could not connect Splitwise" in resp.text


def test_callback_handles_sdk_failure_gracefully(
    client: TestClient,
    fake_sdk: type[FakeSplitwiseSDK],
    patch_db_users: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the token exchange blows up, render the error page (not 5xx)."""

    # Make the next Splitwise() return None from getOAuth2AccessToken.
    class BrokenSDK(FakeSplitwiseSDK):
        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            self.token_response = None

    monkeypatch.setattr("app.splitwise.oauth.Splitwise", BrokenSDK)

    state = mint_state(123)
    resp = client.get("/oauth/callback", params={"code": "abc", "state": state})
    assert resp.status_code == 502
    assert "Could not connect Splitwise" in resp.text
    # No save happened.
    assert patch_db_users["saved"] == []


def test_callback_uses_redirect_uri_from_settings(
    client: TestClient,
    fake_sdk: type[FakeSplitwiseSDK],
    patch_db_users: dict[str, Any],
) -> None:
    """The SDK should be called with the configured PUBLIC_BASE_URL redirect."""
    from tests.splitwise.conftest import TEST_PUBLIC_BASE_URL

    state = mint_state(55)
    client.get("/oauth/callback", params={"code": "thecode", "state": state})

    # We expect one SDK instance from the oauth flow.
    assert FakeSplitwiseSDK.instances, "no SDK instance was constructed"
    sdk = FakeSplitwiseSDK.instances[-1]
    exchange_calls = [c for c in sdk.calls if c[0] == "getOAuth2AccessToken"]
    assert exchange_calls
    code_arg, redirect_arg = exchange_calls[0][1]
    assert code_arg == "thecode"
    assert redirect_arg == f"{TEST_PUBLIC_BASE_URL}/oauth/callback"


def test_callback_does_not_log_code_or_token(
    client: TestClient,
    fake_sdk: type[FakeSplitwiseSDK],
    patch_db_users: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sanity: neither the OAuth code nor the access token should appear in logs."""
    import logging

    caplog.set_level(logging.DEBUG)
    state = mint_state(33)
    client.get("/oauth/callback", params={"code": "supersecret-code", "state": state})
    text = caplog.text
    assert "supersecret-code" not in text
    assert "swt-fake" not in text


def test_callback_handles_current_user_failure(
    client: TestClient,
    fake_sdk: type[FakeSplitwiseSDK],
    patch_db_users: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If we exchange the code but can't identify the user, render an error."""

    class NoUserSDK(FakeSplitwiseSDK):
        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            self.current_user = None

    monkeypatch.setattr("app.splitwise.oauth.Splitwise", NoUserSDK)

    state = mint_state(123)
    resp = client.get("/oauth/callback", params={"code": "abc", "state": state})
    assert resp.status_code == 502
    assert patch_db_users["saved"] == []


# Touch the imported symbol so linters don't flag it as unused — the import
# is what wires the fixture into the module's namespace for test discovery.
_ = FakeSDKUser
