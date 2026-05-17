"""FastAPI app factory + Brick F's TTL-sweep background task."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.db.expenses import sweep_expired_pending
from app.logging_setup import configure_logging
from app.splitwise import splitwise_router
from app.telegram import telegram_router

# How often the background task purges expired ``expenses_pending`` rows.
# Overridable from tests via ``create_app(sweep_interval_seconds=...)``.
SWEEP_INTERVAL_SECONDS: float = 60.0


async def _sweep_loop(interval_seconds: float) -> None:
    """Run :func:`sweep_expired_pending` forever on a timer.

    Errors are logged and the loop continues — a hiccup against Supabase
    must not take the whole bot down. Cancellation (on app shutdown) is
    re-raised so FastAPI can await us cleanly.
    """
    log = logging.getLogger(__name__)
    log.info("ttl-sweep started interval=%ss", interval_seconds)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            swept = await sweep_expired_pending()
            if swept:
                log.info("ttl-sweep ran swept=%d", swept)
        except asyncio.CancelledError:
            log.info("ttl-sweep cancelled")
            raise
        except Exception:  # noqa: BLE001 — never let the loop die quietly
            log.exception("ttl-sweep tick failed; will retry")


def create_app(*, sweep_interval_seconds: float | None = None) -> FastAPI:
    """Build the FastAPI app.

    Args:
        sweep_interval_seconds: Optional override for the TTL-sweep
            cadence. Tests pass a small value to verify the loop runs;
            production uses the module-level default.
    """
    configure_logging(settings.log_level)
    log = logging.getLogger(__name__)
    log.info("starting auto-split (base_url=%s)", settings.public_base_url or "<unset>")

    interval = (
        sweep_interval_seconds
        if sweep_interval_seconds is not None
        else SWEEP_INTERVAL_SECONDS
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Spawn the TTL-sweep task on startup; cancel it on shutdown."""
        task = asyncio.create_task(_sweep_loop(interval), name="ttl-sweep")
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("ttl-sweep task exited with an unexpected error")

    # Disable FastAPI's auto-generated docs / schema endpoints in production.
    # /docs (Swagger), /redoc, and /openapi.json would otherwise be publicly
    # reachable on the ngrok URL — they don't leak secrets but they do
    # advertise the full route inventory + request shapes to anyone who
    # finds the URL. We only have three routes; nobody needs a UI for them.
    app = FastAPI(
        title="auto-split",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(telegram_router)
    app.include_router(splitwise_router)

    return app


app = create_app()
