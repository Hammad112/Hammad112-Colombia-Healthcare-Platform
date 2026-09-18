"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError

from src.api import health
from src.api.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    UnhandledErrorMiddleware,
)
from src.api.review import router as review_router
from src.audit.service import anchor_chain
from src.core.config import get_settings
from src.core.db import dispose_engine, get_sessionmaker
from src.core.logging import configure_logging, get_logger

MAX_REQUEST_BODY_BYTES = 1_000_000

log = get_logger(__name__)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    log.info(
        "startup",
        app_env=settings.app_env,
        allow_real_patient_data=settings.allow_real_patient_data,
    )
    await _anchor_audit_chain()
    yield
    await _anchor_audit_chain()
    await dispose_engine()


async def _anchor_audit_chain() -> None:
    """Witness the audit chain's tip, so entries written since the last anchor
    cannot be removed unnoticed (see src/audit/service.py).

    Anchoring once per process lifetime bounds the exposure to one process's
    worth of entries. The scheduled anchoring that shortens that window arrives
    with background jobs in M2. A failure here must not stop the API from
    serving, so it is logged rather than raised.
    """
    try:
        async with get_sessionmaker()() as session, session.begin():
            await anchor_chain(session)
    except SQLAlchemyError:
        log.exception("audit.anchor_failed")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    # The OpenAPI document lists the review routes, so it is served only where
    # those routes are enabled.
    interactive_docs = settings.synthetic_data_mode

    app = FastAPI(
        title="Clinic Scheduler API",
        version="0.1.0",
        lifespan=_lifespan,
        docs_url="/docs" if interactive_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if interactive_docs else None,
    )

    # add_middleware wraps previously added middleware, so the last added runs first.
    app.add_middleware(RateLimitMiddleware, requests_per_minute=settings.rate_limit_per_minute)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)
    app.add_middleware(UnhandledErrorMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    app.include_router(review_router)
    return app
