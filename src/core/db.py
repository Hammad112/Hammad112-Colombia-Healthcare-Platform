"""Declarative base, engine and session management.

The runtime engine connects as the application role (`app_db_*` settings),
never as the schema owner. psycopg 3 is the driver throughout, because the
LangGraph Postgres checkpointer also requires it.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from enum import StrEnum

from sqlalchemy import Enum, MetaData, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from src.core.config import get_settings

# Deterministic constraint names, so migrations and error messages are stable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Shared declarative base. Each model sets its schema explicitly."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def string_enum(enum_class: type[StrEnum], constraint_name: str) -> Enum:
    """Store a StrEnum as its string value, guarded by a CHECK constraint.

    A VARCHAR plus CHECK is used instead of a native PostgreSQL enum type
    because adding a value then needs only a constraint change. With a native
    enum, `ALTER TYPE ... ADD VALUE` cannot use the new value in the same
    transaction that adds it, which complicates migrations.
    """
    return Enum(
        enum_class,
        name=constraint_name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
        length=max(len(member.value) for member in enum_class),
    )


def configure_event_loop_policy() -> None:
    """Select an event loop that psycopg's async mode can use.

    psycopg cannot run async connections on Windows' default proactor loop.
    Call this before `asyncio.run` in every entry point that opens an async
    connection. It has no effect on other platforms.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Process-wide engine for the application role, created on first use.

    An engine is bound to the event loop that first uses it. Call
    `dispose_engine` before reusing it from a different loop.
    """
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().app_database_url, pool_pre_ping=True)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """Session for one request: commits if the endpoint returns, rolls back if it raises.

    Read requests also commit, because patient-data reads write audit entries.
    Use through `src.api.dependencies.SessionDep`, which closes the session before
    the response is sent.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def count_rows(session: AsyncSession, model: type[Base]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)
