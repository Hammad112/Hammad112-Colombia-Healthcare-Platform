"""LangGraph checkpoint storage in PostgreSQL (ADR-03).

M0 provides the storage only. No conversation graph exists yet; that is M3.

Checkpoints are kept in their own `conversation` schema, selected through the
connection's `search_path`, because the LangGraph tables are created without a
schema qualifier. Keeping them apart from `app` matters because checkpoints
will contain patient data and need their own retention handling: the
open-source checkpointer never deletes old checkpoints by itself.

The owner account creates the tables (`ensure_checkpoint_schema`); the runtime
role only reads and writes them (`open_checkpointer`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from src.core.config import Settings

CHECKPOINT_SCHEMA = "conversation"


def checkpoint_conninfo(settings: Settings, *, admin: bool) -> str:
    return make_conninfo(
        settings.conninfo(admin=admin), options=f"-c search_path={CHECKPOINT_SCHEMA}"
    )


async def ensure_checkpoint_schema(settings: Settings) -> None:
    """Create or upgrade the checkpoint tables and grant the runtime role access.

    Idempotent. `AsyncPostgresSaver.setup` records which of its own migrations
    have run in the `checkpoint_migrations` table.
    """
    schema = sql.Identifier(CHECKPOINT_SCHEMA)
    role = sql.Identifier(settings.app_db_user)
    async with await AsyncConnection.connect(
        settings.conninfo(admin=True), autocommit=True
    ) as conn:
        await conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema))

    async with AsyncPostgresSaver.from_conn_string(
        checkpoint_conninfo(settings, admin=True)
    ) as saver:
        await saver.setup()

    async with await AsyncConnection.connect(
        settings.conninfo(admin=True), autocommit=True
    ) as conn:
        await conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, role))
        await conn.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(
                schema, role
            )
        )


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[AsyncPostgresSaver]:
    """A checkpointer connected as the runtime role. The schema must already exist."""
    async with AsyncPostgresSaver.from_conn_string(
        checkpoint_conninfo(settings, admin=False)
    ) as saver:
        yield saver
