"""Splitwise OAuth 2.0 entry points.

Two things live here:

* :func:`build_auth_url` — given a state token (from :mod:`app.splitwise.state`),
  produce the URL we hand the user to start the Splitwise login flow.
* :func:`exchange_code` — POST the authorization code back to Splitwise to
  redeem an access token, then call ``getCurrentUser`` to pin down the
  ``splitwise_user_id`` we need for ``save_user_token``.
* :data:`router` — FastAPI router exposing ``GET /oauth/callback``.

OAuth endpoints per CLAUDE.md:
  * Authorize: ``https://secure.splitwise.com/oauth/authorize``
  * Token:     ``https://secure.splitwise.com/oauth/token``

(The SDK uses ``www.splitwise.com`` for the same endpoints; both alias to
the same backend, but we follow the documented spec.)
"""

from __future__ import annotations

import asyncio
import html
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from splitwise import Splitwise

from app.config import settings
from app.splitwise.state import InvalidState, verify_state
from app.splitwise.storage import save_user_token

log = logging.getLogger(__name__)

# Public Splitwise OAuth 2.0 endpoints.
_AUTHORIZE_URL = "https://secure.splitwise.com/oauth/authorize"


def _redirect_uri() -> str:
    """Compose the OAuth redirect URI from ``PUBLIC_BASE_URL``.

    Must match the value registered at
    https://secure.splitwise.com/oauth_clients exactly.
    """
    base = settings.public_base_url.rstrip("/")
    if not base:
        raise RuntimeError(
            "PUBLIC_BASE_URL is not set; cannot build OAuth redirect URI"
        )
    return f"{base}/oauth/callback"


def build_auth_url(state: str) -> str:
    """Build the URL the user should visit to begin the Splitwise OAuth flow.

    ``state`` should come from :func:`app.splitwise.state.mint_state` so the
    callback can identify which Telegram user just authed.
    """
    client_id = settings.splitwise_client_id.get_secret_value()
    if not client_id:
        raise RuntimeError("SPLITWISE_CLIENT_ID is not set")
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "state": state,
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> tuple[int, str]:
    """Exchange an authorization ``code`` for ``(splitwise_user_id, access_token)``.

    Runs the (sync) SDK calls in a thread so we don't block the loop.
    Never logs the code or the resulting token in plaintext.
    """
    client_id = settings.splitwise_client_id.get_secret_value()
    client_secret = settings.splitwise_client_secret.get_secret_value()
    if not client_id or not client_secret:
        raise RuntimeError("Splitwise OAuth credentials are not configured")

    def _redeem() -> tuple[int, str]:
        sdk = Splitwise(client_id, client_secret)
        token_resp = sdk.getOAuth2AccessToken(code, _redirect_uri())
        if not token_resp or "access_token" not in token_resp:
            raise RuntimeError("Splitwise token endpoint returned no access_token")
        access_token = token_resp["access_token"]
        # Configure the SDK with the freshly minted token so we can ask
        # who the user is. We need the splitwise_user_id alongside the
        # token for the users table.
        sdk.setOAuth2AccessToken(
            {
                "access_token": access_token,
                "token_type": token_resp.get("token_type", "bearer"),
            }
        )
        user = sdk.getCurrentUser()
        if user is None:
            raise RuntimeError("Splitwise getCurrentUser returned None after auth")
        return int(user.getId()), access_token

    return await asyncio.to_thread(_redeem)


router = APIRouter(tags=["splitwise"])


# Minimal HTML pages — the user reaches these once per Splitwise account,
# usually on mobile. Keep them small and self-contained; no asset hosting.
_SUCCESS_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Splitwise connected</title>
<style>
  body{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:32em;margin:4em auto;padding:0 1em;color:#1a1a1a}
  h1{font-size:1.4em;margin:0 0 .5em}
  p{margin:.5em 0}
  .ok{color:#137333}
</style></head>
<body>
  <h1 class="ok">Splitwise connected.</h1>
  <p>You can close this tab and head back to Telegram. The bot is ready to add expenses for you.</p>
</body></html>
"""

_ERROR_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Splitwise connection failed</title>
<style>
  body{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:32em;margin:4em auto;padding:0 1em;color:#1a1a1a}
  h1{font-size:1.4em;margin:0 0 .5em}
  p{margin:.5em 0}
  .err{color:#b3261e}
  code{background:#f1f3f4;padding:.1em .3em;border-radius:.2em}
</style></head>
<body>
  <h1 class="err">Could not connect Splitwise.</h1>
  <p>__REASON__</p>
  <p>DM the bot <code>/start</code> in Telegram to try again.</p>
</body></html>
"""


def _render_error(reason: str) -> str:
    """Render the error page with ``reason`` HTML-escaped.

    We use a placeholder sentinel + ``str.replace`` instead of ``str.format``
    because the embedded CSS contains ``{...}`` blocks that would confuse
    ``.format``. ``reason`` is escaped to be safe with any value Splitwise
    might bounce back at us.
    """
    return _ERROR_PAGE.replace("__REASON__", html.escape(reason))


@router.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> HTMLResponse:
    """Handle the Splitwise OAuth redirect.

    Flow:
      1. If Splitwise sent back an ``error`` query param, render the error page.
      2. Verify the ``state`` token → telegram_user_id (or 400).
      3. Exchange ``code`` for an access token + splitwise_user_id.
      4. Persist via :func:`save_user_token` (encrypts under the hood).
      5. Render a success page.

    We never log ``code`` or the access token. We do log the (verified)
    telegram_user_id and the splitwise_user_id at info level so we can
    trace which user just connected.
    """
    if error:
        # Splitwise-side error (user denied access, etc.) — render and stop.
        log.info("OAuth callback returned error=%s", error)
        return HTMLResponse(
            content=_render_error("Splitwise reported: " + error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing code or state",
        )

    try:
        telegram_user_id = verify_state(state)
    except InvalidState as exc:
        log.warning("OAuth callback rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid or expired state",
        ) from exc

    try:
        splitwise_user_id, access_token = await exchange_code(code)
    except Exception:  # noqa: BLE001 — render a friendly page, log details
        log.exception(
            "OAuth code exchange failed for telegram_user_id=%s", telegram_user_id
        )
        return HTMLResponse(
            content=_render_error(
                "We couldn't redeem the authorization code. "
                "It may have expired — please try again."
            ),
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    await save_user_token(
        telegram_user_id=telegram_user_id,
        splitwise_user_id=splitwise_user_id,
        plain_token=access_token,
    )
    log.info(
        "splitwise oauth ok: telegram_user_id=%s splitwise_user_id=%s",
        telegram_user_id,
        splitwise_user_id,
    )
    return HTMLResponse(content=_SUCCESS_PAGE, status_code=status.HTTP_200_OK)


__all__ = ["build_auth_url", "exchange_code", "router"]
