"""Limit/offset pagination for repository queries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class PageRequest:
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True, slots=True)
class PageResult[T]:
    items: list[T]
    total: int
    limit: int
    offset: int


async def fetch_page(
    session: AsyncSession, statement: Select[Any], page: PageRequest
) -> tuple[list[Any], int]:
    """Run `statement` for one page and count all matching rows.

    Returns the result rows (as `Row` objects) and the total. The count runs as a
    separate query, so under concurrent writes it can differ from what a later
    page would observe.
    """
    total = await session.scalar(
        select(func.count()).select_from(statement.order_by(None).subquery())
    )
    rows = (await session.execute(statement.limit(page.limit).offset(page.offset))).all()
    return list(rows), int(total or 0)
