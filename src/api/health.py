"""Liveness and readiness probes."""

from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from src.api.dependencies import SessionDep, SettingsDep
from src.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["health"])


class Liveness(BaseModel):
    status: str


class Readiness(BaseModel):
    status: str
    database: str
    app_env: str
    real_patient_data_allowed: bool


@router.get("/healthz", response_model=Liveness)
async def healthz() -> Liveness:
    """The process is running. Checks no dependencies."""
    return Liveness(status="ok")


@router.get(
    "/readyz",
    response_model=Readiness,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": Readiness}},
)
async def readyz(session: SessionDep, settings: SettingsDep) -> Readiness | JSONResponse:
    """The database answers as the runtime role. Also reports the real-data gate."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        log.error("readiness_check_failed", error_type=type(exc).__name__, error=str(exc))
        body = Readiness(
            status="unavailable",
            database="unreachable",
            app_env=settings.app_env,
            real_patient_data_allowed=settings.allow_real_patient_data,
        )
        return JSONResponse(body.model_dump(), status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Readiness(
        status="ok",
        database="ok",
        app_env=settings.app_env,
        real_patient_data_allowed=settings.allow_real_patient_data,
    )
