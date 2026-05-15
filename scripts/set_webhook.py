"""One-shot CLI to register our webhook URL with Telegram.

Usage:
    uv run python scripts/set_webhook.py

Reads ``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_WEBHOOK_SECRET``, and
``PUBLIC_BASE_URL`` from ``app.config.settings``. The webhook path is
hard-coded to ``/telegram/webhook`` (must match ``app/telegram/webhook.py``).

What this does:
    1. POSTs to ``https://api.telegram.org/bot<TOKEN>/setWebhook``
       with ``url``, ``secret_token``, and ``allowed_updates=["message","callback_query"]``.
    2. Prints the JSON response from Telegram.

If you need to clear the webhook (e.g. to switch to polling), pass
``--delete``; it will call ``deleteWebhook`` instead.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Make the project root importable when invoked as ``uv run python scripts/set_webhook.py``.
# (Python only adds the script's own directory to sys.path; ``app`` lives one level up.)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402 — must follow sys.path tweak

WEBHOOK_PATH = "/telegram/webhook"
ALLOWED_UPDATES = ["message", "callback_query"]


def _post(url: str, data: dict[str, object]) -> dict[str, object]:
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 — known Telegram URL
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def set_webhook() -> int:
    token = settings.telegram_bot_token.get_secret_value()
    secret = settings.telegram_webhook_secret.get_secret_value()
    base = settings.public_base_url

    missing: list[str] = []
    if not token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not secret:
        missing.append("TELEGRAM_WEBHOOK_SECRET")
    if not base:
        missing.append("PUBLIC_BASE_URL")
    if missing:
        sys.stderr.write(f"missing required settings: {', '.join(missing)}\n")
        return 2

    webhook_url = urllib.parse.urljoin(base.rstrip("/") + "/", WEBHOOK_PATH.lstrip("/"))
    api = f"https://api.telegram.org/bot{token}/setWebhook"
    payload: dict[str, object] = {
        "url": webhook_url,
        "secret_token": secret,
        "allowed_updates": ALLOWED_UPDATES,
        "drop_pending_updates": True,
    }
    print(
        f"POST {api}\n      url={webhook_url}\n      allowed_updates={ALLOWED_UPDATES}"
    )
    resp = _post(api, payload)
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


def delete_webhook() -> int:
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        sys.stderr.write("missing required setting: TELEGRAM_BOT_TOKEN\n")
        return 2
    api = f"https://api.telegram.org/bot{token}/deleteWebhook"
    print(f"POST {api}")
    resp = _post(api, {"drop_pending_updates": True})
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("ok") else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register or clear the Telegram webhook."
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Clear the webhook instead of setting it.",
    )
    args = parser.parse_args()
    return delete_webhook() if args.delete else set_webhook()


if __name__ == "__main__":
    raise SystemExit(main())
