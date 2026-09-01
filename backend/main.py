"""Application factory and entrypoint.

One process. The agent loop lands here as a background task -- it does not need
its own service. Run with: uvicorn backend.main:app --reload
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api import api_router
from .core.config import get_settings
from .core.logging import configure_logging, get_logger
from .repositories import ensure_schema


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    log = get_logger(__name__)

    try:
        ensure_schema()
    except Exception:
        log.error(
            "MySQL unreachable or migrations failed -- is MySQL running and are "
            "MYSQL_HOST/MYSQL_USER/MYSQL_PASSWORD in .env correct?"
        )
        raise

    app = FastAPI(title=settings.app_name, version=settings.version)
    app.include_router(api_router)

    # Demo-day mode: serve the built dashboard from this same process. API
    # routes are registered first, so they win; everything else falls through
    # to the SPA. Absent dist/ (pure-API dev), this is a no-op.
    dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="dashboard")
        log.info("serving dashboard from %s", dist)

    if not settings.razorpay_webhook_secret:
        log.warning("RAZORPAY_WEBHOOK_SECRET unset -- webhook signatures NOT enforced")

    log.info("%s v%s ready (strategy=%s)", settings.app_name, settings.version, settings.default_strategy)
    return app


app = create_app()
