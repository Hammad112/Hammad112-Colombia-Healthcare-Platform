"""Reusable FastAPI dependency declarations."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, get_settings
from src.core.db import get_session
from src.core.pagination import PageRequest

MAX_PAGE_SIZE = 200


def page_request(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE, description="Page size")] = 50,
    offset: Annotated[int, Query(ge=0, description="Rows to skip")] = 0,
) -> PageRequest:
    return PageRequest(limit=limit, offset=offset)


# scope="function" (FastAPI >= 0.121) closes the session when the endpoint returns,
# before the response is sent. With the default scope it would close after the
# response, so a failed commit could lose audit entries for data already delivered.
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
SettingsDep = Annotated[Settings, Depends(get_settings)]
PageDep = Annotated[PageRequest, Depends(page_request)]
