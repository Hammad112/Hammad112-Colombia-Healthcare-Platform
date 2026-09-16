"""Rate limiting (one of the four guardrails the client asked about).

The limiter runs at the edge, before anything reaches conversation logic, so a
request that should be rejected never reaches a component that calls a model.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.middleware import RateLimitMiddleware


def _app(limit: int) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=limit)

    @app.get("/thing")
    async def thing() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def test_requests_beyond_the_limit_are_rejected() -> None:
    with TestClient(_app(limit=5)) as client:
        codes = [client.get("/thing").status_code for _ in range(8)]
    assert codes.count(200) == 5
    assert codes.count(429) == 3


def test_rejection_tells_the_caller_when_to_retry() -> None:
    with TestClient(_app(limit=1)) as client:
        client.get("/thing")
        blocked = client.get("/thing")
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "60"


def test_health_checks_are_never_rate_limited() -> None:
    """A throttled liveness probe would make an orchestrator kill a healthy pod."""
    with TestClient(_app(limit=2)) as client:
        for _ in range(5):
            client.get("/thing")  # exhaust the budget
        codes = [client.get("/healthz").status_code for _ in range(5)]
    assert codes == [200] * 5
