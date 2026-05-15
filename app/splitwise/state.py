"""Stateless OAuth ``state`` parameter, signed with the Fernet key.

We need a way to bind the Splitwise OAuth redirect back to the originating
Telegram user without holding server-side session state. The trick is to
encode ``{"u": telegram_user_id, "exp": <epoch>}`` into a Fernet token and
pass it as the ``state`` query parameter.

Why Fernet and not PyJWT? PyJWT isn't a dependency yet and Fernet already
gives us authenticated encryption with a built-in timestamp + TTL — exactly
what we need. We additionally check our own ``exp`` claim so an attacker who
somehow got a Fernet token still can't replay an old state forever.

Lifetimes are 10 minutes, which is generous for "click link, log in, approve."
"""

from __future__ import annotations

import json
import time

from cryptography.fernet import InvalidToken

from app.splitwise.tokens import _fernet

# 10 minutes. Splitwise's hosted login flow is short; if the user takes
# longer than this we'd rather restart cleanly than handle a stale callback.
_STATE_TTL_SECONDS = 600


class InvalidState(Exception):
    """Raised when an OAuth ``state`` value is missing, tampered, or expired."""


def mint_state(telegram_user_id: int) -> str:
    """Return a signed, time-bound state token for ``telegram_user_id``.

    The token is opaque to the user and to Splitwise — it round-trips as
    the ``state`` query param and is verified on callback.
    """
    payload = {"u": int(telegram_user_id), "exp": int(time.time()) + _STATE_TTL_SECONDS}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _fernet().encrypt(raw).decode("ascii")


def verify_state(token: str) -> int:
    """Decode and validate ``token``; return the embedded ``telegram_user_id``.

    Raises :class:`InvalidState` for any failure mode (bad signature,
    malformed payload, expired). The caller MUST treat a raise as a hard
    400 — never trust an unverified state.
    """
    if not token:
        raise InvalidState("missing state")
    try:
        # Belt-and-braces: also use Fernet's own TTL check. If our exp logic
        # below disagrees, the more restrictive one wins (which is fine).
        raw = _fernet().decrypt(token.encode("ascii"), ttl=_STATE_TTL_SECONDS)
    except InvalidToken as exc:
        raise InvalidState("state signature invalid or expired") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidState("state payload is not valid JSON") from exc

    user_id = payload.get("u")
    exp = payload.get("exp")
    if not isinstance(user_id, int) or not isinstance(exp, int):
        raise InvalidState("state payload missing required claims")
    if exp < int(time.time()):
        raise InvalidState("state expired")
    return user_id


__all__ = ["InvalidState", "mint_state", "verify_state"]
