from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
)
from src.api.routers import health
from src.core.config import get_settings
from src.core.db import dispose_engine
from src.core.logging import configure_logging, get_logger


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    get_logger(__name__).info(
        "startup",
        app_env=settings.app_env,
        allow_real_patient_data=settings.allow_real_patient_data,
    )
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Clinic Scheduler",
        version="0.1.0",
        lifespan=lifespan,
        # Docs are open in local/CI only; staff APIs get real auth in M11.
        docs_url="/docs" if settings.app_env in ("local", "ci") else None,
        redoc_url=None,
    )
    app.add_middleware(RateLimitMiddleware, requests_per_minute=settings.rate_limit_per_minute)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    return app


app = create_app()
