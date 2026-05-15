"""``build_auth_url`` produces the right query string."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from app.splitwise.oauth import build_auth_url

from .conftest import TEST_CLIENT_ID, TEST_PUBLIC_BASE_URL


def test_build_auth_url_components() -> None:
    url = build_auth_url("the-state-token")
    parsed = urlparse(url)
    # Splitwise docs say secure.splitwise.com/oauth/authorize.
    assert parsed.scheme == "https"
    assert parsed.netloc == "secure.splitwise.com"
    assert parsed.path == "/oauth/authorize"

    qs = parse_qs(parsed.query)
    assert qs["client_id"] == [TEST_CLIENT_ID]
    assert qs["redirect_uri"] == [f"{TEST_PUBLIC_BASE_URL}/oauth/callback"]
    assert qs["response_type"] == ["code"]
    assert qs["state"] == ["the-state-token"]


def test_build_auth_url_url_encodes_state() -> None:
    # Even though Fernet tokens are URL-safe, make sure we encode just in case
    # a future state value contains reserved chars.
    url = build_auth_url("a state with spaces & ampersand")
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert qs["state"] == ["a state with spaces & ampersand"]
