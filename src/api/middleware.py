"""Edge middleware: request identity, body limits, and rate limiting.

These are the M0 framework for two of the four guardrails the client asked
about (input validation, rate limiting). They apply before any request reaches
conversation logic, which is the point: a request that should be rejected must
never reach the part of the system that calls a model.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.audit.context import AuditContext, set_context
from src.core.logging import get_logger

log = get_logger(__name__)

MAX_BODY_BYTES = 1_000_000  # 1 MB; media is fetched out-of-band, not posted inline


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id and seeds the audit context for the request."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        set_context(
            AuditContext(
                actor_kind="system",
                purpose="http_request",
                request_id=request_id,
                ip=request.client.host if request.client else None,
            )
        )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Rejects oversized bodies before they are parsed."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "Payload too large"}, status_code=413)
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window-free sliding token check, keyed by client identity.

    ponytail: in-process deques, so the limit is per worker rather than global.
    Correct for M0 (single worker) and for CI. Swap the backend for a Redis
    token bucket before M4, when multiple workers and real provider traffic
    arrive — the interface here does not change.
    """

    def __init__(self, app: object, requests_per_minute: int) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.limit = requests_per_minute
        self.window = 60.0
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _key(self, request: Request) -> str:
        return request.client.host if request.client else "unknown"

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in ("/healthz", "/readyz"):
            return await call_next(request)

        now = time.monotonic()
        hits = self._hits[self._key(request)]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            log.warning("rate_limited", path=request.url.path)
            return JSONResponse(
                {"detail": "Too many requests"},
                status_code=429,
                headers={"Retry-After": "60"},
            )
        hits.append(now)
        return await call_next(request)
