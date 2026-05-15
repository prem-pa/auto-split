import logging

from fastapi import FastAPI

from app.config import settings
from app.logging_setup import configure_logging
from app.splitwise import splitwise_router
from app.telegram import telegram_router


def create_app() -> FastAPI:
    configure_logging(settings.log_level)
    log = logging.getLogger(__name__)
    log.info("starting auto-split (base_url=%s)", settings.public_base_url or "<unset>")

    app = FastAPI(title="auto-split")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(telegram_router)
    app.include_router(splitwise_router)

    return app


app = create_app()
