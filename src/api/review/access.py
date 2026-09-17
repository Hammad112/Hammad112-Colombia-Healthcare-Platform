"""Access control for the review API.

Staff authentication does not exist until M11. Until then the review API is
available only in synthetic-data mode (APP_ENV local or ci, and the real-data
gate closed). In any other configuration a GET to a review route responds 404,
and the OpenAPI document that would list the routes is not served. Routing still
answers other HTTP methods on those paths with 405, as for any existing route.

Requests that pass are attributed to a fixed local reviewer in the audit log.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from src.api.dependencies import SettingsDep
from src.audit.context import ActorKind, AuditContext, set_context

REVIEWER_ID = "local-reviewer"
REVIEW_PURPOSE = "synthetic_data_review"
REVIEW_CHANNEL = "review_api"


async def require_synthetic_review(request: Request, settings: SettingsDep) -> None:
    if not settings.synthetic_data_mode:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    # Replaces the middleware's default context for the rest of this request.
    # When the request ends, the middleware resets the variable to its value
    # from before the request, which discards this one as well.
    set_context(
        AuditContext(
            actor_kind=ActorKind.STAFF,
            actor_id=REVIEWER_ID,
            purpose=REVIEW_PURPOSE,
            channel=REVIEW_CHANNEL,
            request_id=request.state.request_id,
            ip=request.client.host if request.client else None,
        )
    )
