"""Shared fixtures for Brick C (Splitwise) tests.

Goals:
    * Pin a known Fernet encryption key + Splitwise OAuth creds + public base URL
      so the state/token round-trip and OAuth-URL tests are deterministic.
    * Never hit the real Splitwise API or Supabase — fixtures monkeypatch the
      SDK constructor and ``app.db.users`` functions.
    * Build a FastAPI ``TestClient`` from ``create_app()`` so the
      ``/oauth/callback`` route assertions exercise the real wiring.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

# Stable test fixtures — keep these the same across the suite.
TEST_FERNET_KEY = Fernet.generate_key().decode()
TEST_CLIENT_ID = "test-client-id"
TEST_CLIENT_SECRET = "test-client-secret"
TEST_PUBLIC_BASE_URL = "https://bot.example.test"


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force test-only secrets onto the global ``settings`` singleton."""
    from app.config import settings

    monkeypatch.setattr(settings, "token_encryption_key", SecretStr(TEST_FERNET_KEY))
    monkeypatch.setattr(settings, "splitwise_client_id", SecretStr(TEST_CLIENT_ID))
    monkeypatch.setattr(
        settings, "splitwise_client_secret", SecretStr(TEST_CLIENT_SECRET)
    )
    monkeypatch.setattr(settings, "public_base_url", TEST_PUBLIC_BASE_URL)
    # Webhook secret is required by Brick B's router which is also mounted —
    # set something so app boot doesn't fail any sanity checks down the line.
    monkeypatch.setattr(
        settings, "telegram_webhook_secret", SecretStr("test-webhook-secret")
    )
    monkeypatch.setattr(
        settings, "telegram_bot_token", SecretStr("123456:TEST-NOT-REAL")
    )


@pytest.fixture(autouse=True)
def _reset_fernet_cache() -> Iterator[None]:
    """Clear the cached Fernet so settings overrides take effect each test."""
    from app.splitwise.tokens import _fernet

    _fernet.cache_clear()
    yield
    _fernet.cache_clear()


class FakeSDKUser:
    """Stand-in for ``splitwise.user.CurrentUser`` (and Friend)."""

    def __init__(
        self,
        uid: int,
        first: str = "Test",
        last: str = "User",
        email: str = "t@example.test",
    ) -> None:
        self._id = uid
        self._first = first
        self._last = last
        self._email = email

    def getId(self) -> int:
        return self._id

    def getFirstName(self) -> str:
        return self._first

    def getLastName(self) -> str:
        return self._last

    def getEmail(self) -> str:
        return self._email


class FakeSDKGroup:
    def __init__(self, gid: int, name: str, members: list[FakeSDKUser]) -> None:
        self._id = gid
        self._name = name
        self._members = members

    def getId(self) -> int:
        return self._id

    def getName(self) -> str:
        return self._name

    def getMembers(self) -> list[FakeSDKUser]:
        return self._members


class FakeSDKExpense:
    def __init__(self, eid: int) -> None:
        self._id = eid

    def getId(self) -> int:
        return self._id


class FakeSplitwiseSDK:
    """Stand-in for ``splitwise.Splitwise`` constructor.

    Each test wires up its own return values directly on the instance after
    construction (via the ``last_instance`` reference on the patch factory).
    """

    instances: list[FakeSplitwiseSDK] = []

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token: dict[str, Any] | None = None
        # Pre-canned return values; tests overwrite as needed.
        self.token_response: dict[str, Any] | None = {
            "access_token": "swt-fake",
            "token_type": "bearer",
        }
        self.current_user: FakeSDKUser | None = FakeSDKUser(99, "Cur", "User")
        self.friends: list[FakeSDKUser] = []
        self.groups: list[FakeSDKGroup] = []
        self.create_expense_result: tuple[FakeSDKExpense | None, Any] = (
            FakeSDKExpense(7777),
            None,
        )
        # Recorded calls for assertions.
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        FakeSplitwiseSDK.instances.append(self)

    # --- the SDK methods we use -----------------------------------------

    def setOAuth2AccessToken(self, token: dict[str, Any]) -> None:
        self.calls.append(("setOAuth2AccessToken", (token,), {}))
        self.access_token = token

    def getOAuth2AccessToken(
        self, code: str, redirect_uri: str
    ) -> dict[str, Any] | None:
        self.calls.append(("getOAuth2AccessToken", (code, redirect_uri), {}))
        return self.token_response

    def getCurrentUser(self) -> FakeSDKUser | None:
        self.calls.append(("getCurrentUser", (), {}))
        return self.current_user

    def getFriends(self) -> list[FakeSDKUser]:
        self.calls.append(("getFriends", (), {}))
        return self.friends

    def getGroups(self) -> list[FakeSDKGroup]:
        self.calls.append(("getGroups", (), {}))
        return self.groups

    def createExpense(self, expense: Any) -> tuple[FakeSDKExpense | None, Any]:
        self.calls.append(("createExpense", (expense,), {}))
        return self.create_expense_result


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeSplitwiseSDK]]:
    """Patch ``splitwise.Splitwise`` everywhere it's imported.

    Tests can read the captured instances via ``FakeSplitwiseSDK.instances``.
    """
    FakeSplitwiseSDK.instances = []
    monkeypatch.setattr("app.splitwise.client.Splitwise", FakeSplitwiseSDK)
    monkeypatch.setattr("app.splitwise.oauth.Splitwise", FakeSplitwiseSDK)
    yield FakeSplitwiseSDK
    FakeSplitwiseSDK.instances = []


@pytest.fixture
def patch_db_users(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``app.db.users`` calls with in-memory stand-ins.

    Returns a state dict the test can inspect / pre-populate::

        {"saved": [(tid, encrypted_token, swid), ...], "users": {tid: User}}
    """
    from app.db.models import User

    state: dict[str, Any] = {
        "saved": [],
        "users": {},  # telegram_user_id -> User
    }

    async def fake_set_user_token(
        telegram_user_id: int,
        encrypted_token: str,
        splitwise_user_id: int,
    ) -> None:
        state["saved"].append((telegram_user_id, encrypted_token, splitwise_user_id))
        # Update / insert the in-memory user too so subsequent get_user works.
        existing = state["users"].get(telegram_user_id)
        if existing is None:
            state["users"][telegram_user_id] = User(
                telegram_user_id=telegram_user_id,
                splitwise_access_token=encrypted_token,
                splitwise_user_id=splitwise_user_id,
            )
        else:
            state["users"][telegram_user_id] = existing.model_copy(
                update={
                    "splitwise_access_token": encrypted_token,
                    "splitwise_user_id": splitwise_user_id,
                }
            )

    async def fake_get_user(telegram_user_id: int) -> User | None:
        return state["users"].get(telegram_user_id)

    monkeypatch.setattr("app.splitwise.storage.set_user_token", fake_set_user_token)
    monkeypatch.setattr("app.splitwise.storage.get_user", fake_get_user)
    return state


@pytest.fixture
def client() -> Iterator[TestClient]:
    """A ``TestClient`` built from a fresh ``create_app()`` invocation."""
    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c
