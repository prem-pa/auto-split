"""Tests for ``app.db.client``."""

from __future__ import annotations

import pytest
from pydantic import SecretStr


def test_get_supabase_raises_when_url_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings
    from app.db import client

    client.reset_supabase_cache()
    monkeypatch.setattr(settings, "supabase_url", "")
    monkeypatch.setattr(settings, "supabase_service_role_key", SecretStr("anything"))
    with pytest.raises(RuntimeError, match="SUPABASE_URL"):
        client.get_supabase()
    client.reset_supabase_cache()


def test_get_supabase_raises_when_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings
    from app.db import client

    client.reset_supabase_cache()
    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co")
    monkeypatch.setattr(settings, "supabase_service_role_key", SecretStr(""))
    with pytest.raises(RuntimeError, match="SUPABASE_SERVICE_ROLE_KEY"):
        client.get_supabase()
    client.reset_supabase_cache()
