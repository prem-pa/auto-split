"""Brick C — Splitwise OAuth + API surface.

Public interface other bricks should import from here::

    from app.splitwise import (
        splitwise_router,       # FastAPI router, exposes GET /oauth/callback
        build_auth_url,         # str(state) -> URL for Splitwise login
        mint_state,             # int(telegram_user_id) -> signed state token
        verify_state,           # str(state) -> telegram_user_id; raises InvalidState
        SplitwiseClient,        # per-user async API wrapper
        Split,                  # pydantic model
        SplitwiseUser,
        SplitwiseGroup,
        encrypt,
        decrypt,
        save_user_token,
        load_user_token,
    )
"""

from app.splitwise.client import SplitwiseAPIError, SplitwiseClient
from app.splitwise.models import Split, SplitwiseGroup, SplitwiseUser
from app.splitwise.oauth import build_auth_url, exchange_code
from app.splitwise.oauth import router as splitwise_router
from app.splitwise.state import InvalidState, mint_state, verify_state
from app.splitwise.storage import load_user_token, save_user_token
from app.splitwise.tokens import decrypt, encrypt

__all__ = [
    "InvalidState",
    "Split",
    "SplitwiseAPIError",
    "SplitwiseClient",
    "SplitwiseGroup",
    "SplitwiseUser",
    "build_auth_url",
    "decrypt",
    "encrypt",
    "exchange_code",
    "load_user_token",
    "mint_state",
    "save_user_token",
    "splitwise_router",
    "verify_state",
]
