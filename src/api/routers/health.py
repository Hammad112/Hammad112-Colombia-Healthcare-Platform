from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, get_settings
from src.core.db import get_session
from src.core.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up. No dependencies checked."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Readiness: the database answers and the compliance gate is reported."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        # Log the cause: a readiness probe that hides why it failed turns a
        # five-minute diagnosis into an hour of guessing.
        log.error("readiness_check_failed", error=str(exc), error_type=type(exc).__name__)
        return JSONResponse(
            {"status": "degraded", "database": "unreachable"}, status_code=503
        )
    return {
        "status": "ok",
        "database": "ok",
        "app_env": settings.app_env,
        # ADR-11 gate, surfaced so it is impossible to forget which mode we are in.
        "real_patient_data_allowed": settings.allow_real_patient_data,
    }
