"""Process-wide Supabase client singleton.

``supabase-py`` exposes a sync ``Client``; we wrap call sites in
``asyncio.to_thread`` elsewhere so the rest of the app stays async.

The client is built lazily so importing this module does not require
real credentials — important for unit tests that mock the client.
"""

from __future__ import annotations

from functools import lru_cache

from supabase import Client, create_client

from app.config import settings


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    """Return the process-wide Supabase ``Client``.

    Raises ``RuntimeError`` if either ``SUPABASE_URL`` or
    ``SUPABASE_SERVICE_ROLE_KEY`` is unset. We use the service-role key
    by design — this is backend-only code and must bypass RLS to
    operate across all rows.
    """
    url = settings.supabase_url
    key = settings.supabase_service_role_key.get_secret_value()
    missing: list[str] = []
    if not url:
        missing.append("SUPABASE_URL")
    if not key:
        missing.append("SUPABASE_SERVICE_ROLE_KEY")
    if missing:
        raise RuntimeError(
            "Supabase client requires: " + ", ".join(missing) + " (set in .env)"
        )
    return create_client(url, key)


def reset_supabase_cache() -> None:
    """Drop the cached client. Used by tests after monkey-patching settings."""
    get_supabase.cache_clear()
