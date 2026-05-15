"""Tests for ``app.config.Settings`` parsing rules.

We don't test the trivial fields (everything that's just ``SecretStr("")`` or
``str = ""``). What's worth covering here is anything with a custom
validator or alias — places where a typo in the spec would silently
load the wrong value.

We drive these via environment variables (``monkeypatch.setenv``) rather
than constructor kwargs, because that's the path production actually uses
and it exercises pydantic-settings' env-name resolution end-to-end.
"""

from __future__ import annotations

import pytest

from app.config import Settings


def _make(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Build a Settings instance from a clean env (no .env file)."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_allowed_user_ids_default_is_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.telegram_allowed_user_ids == []


def test_allowed_user_ids_parses_csv_string(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _make(monkeypatch, TELEGRAM_ALLOWED_USER_IDS="12345,67890,11")
    assert s.telegram_allowed_user_ids == [12345, 67890, 11]


def test_allowed_user_ids_strips_whitespace_and_trailing_commas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _make(monkeypatch, TELEGRAM_ALLOWED_USER_IDS="  1, 2 , 3,  , ")
    assert s.telegram_allowed_user_ids == [1, 2, 3]


def test_allowed_user_ids_accepts_native_list_in_code() -> None:
    """Passing a list directly (e.g. in tests) bypasses the CSV parser."""
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        telegram_allowed_user_ids=[7, 8, 9],
    )
    assert s.telegram_allowed_user_ids == [7, 8, 9]


def test_allowed_user_ids_rejects_non_integer_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError):
        _make(monkeypatch, TELEGRAM_ALLOWED_USER_IDS="123,notanumber")
