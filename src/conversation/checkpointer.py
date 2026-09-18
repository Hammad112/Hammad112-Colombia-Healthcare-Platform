"""LangGraph checkpoint storage in PostgreSQL (ADR-03).

M0 provides the storage only. No conversation graph exists yet; that is M3.

Checkpoints are kept in their own `conversation` schema, selected through the
connection's `search_path`, because the LangGraph tables are created without a
schema qualifier.

Checkpoints will hold patient data, so two things are true of them from M0,
before the graph that writes them exists:

Encryption
    `open_checkpointer` wraps the serializer in LangGraph's
    `EncryptedSerializer`, so values it handles are AES-256-GCM ciphertext in
    the same format as the encrypted patient columns, under the same key.
    LangGraph's own AES helper needs pycryptodome and a separate key in an
    environment variable; using `src.core.crypto` instead keeps one cipher and
    one key for all patient data.

    This does not cover the whole checkpoint. `AsyncPostgresSaver.aput` writes
    channel values that are None, str, int, float or bool directly into the
    `checkpoints` JSONB column and sends only the rest through the serializer,
    so a primitive channel value is stored in the clear. Patient data therefore
    must not be held in a primitive channel; it belongs in message objects or
    other structured values, which do go through the serializer.
    `test_inlined_primitive_channel_values_are_not_encrypted` pins this.

Retention
    The open-source checkpointer never deletes anything. `sweep_expired_threads`
    deletes every thread untouched for `checkpoint_retention_days`, using a
    `created_at` column this module adds to the LangGraph tables. It is called
    by the scheduled job that arrives with background jobs in M2; until then a
    run of `python -m src.conversation.checkpointer` performs one sweep.

The owner account creates the tables (`ensure_checkpoint_schema`); the runtime
role only reads and writes them (`open_checkpointer`).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from src.core.config import Settings, get_settings
from src.core.crypto import decrypt_bytes, encrypt_bytes
from src.core.db import configure_event_loop_policy

CHECKPOINT_SCHEMA = "conversation"
# The LangGraph tables keyed by thread_id, in the order they must be deleted;
# they have no foreign keys between them, so the order is ours to choose.
_THREAD_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")
_CIPHER_NAME = "clinic-aesgcm-v1"


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

    # LangGraph records no timestamps, so retention needs one. The column is
    # additive and its default fills itself in, so the checkpointer neither sees
    # it nor breaks on it, and its own migrations keep working.
    async with await AsyncConnection.connect(
        settings.conninfo(admin=True), autocommit=True
    ) as conn:
        for table in _THREAD_TABLES:
            await conn.execute(
                sql.SQL(
                    "ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS"
                    " created_at timestamptz NOT NULL DEFAULT now()"
                ).format(schema, sql.Identifier(table))
            )

    async with await AsyncConnection.connect(
        settings.conninfo(admin=True), autocommit=True
    ) as conn:
        await conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, role))
        await conn.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(
                schema, role
            )
        )


class _ClinicCipher:
    """Adapts `src.core.crypto` to LangGraph's `CipherProtocol`.

    The name is stored next to each value so a later cipher can be introduced
    without guessing how existing values were encrypted.
    """

    def encrypt(self, plaintext: bytes) -> tuple[str, bytes]:
        return _CIPHER_NAME, encrypt_bytes(plaintext)

    def decrypt(self, ciphername: str, ciphertext: bytes) -> bytes:
        if ciphername != _CIPHER_NAME:
            raise ValueError(f"Unsupported checkpoint cipher: {ciphername}")
        return decrypt_bytes(ciphertext)


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[AsyncPostgresSaver]:
    """A checkpointer connected as the runtime role, encrypting what it stores.

    The schema must already exist. Values written without this serializer cannot
    be read back through it, and the reverse, so it is applied everywhere or
    nowhere.
    """
    async with AsyncPostgresSaver.from_conn_string(
        checkpoint_conninfo(settings, admin=False),
        serde=EncryptedSerializer(_ClinicCipher()),
    ) as saver:
        yield saver


async def sweep_expired_threads(settings: Settings) -> int:
    """Delete threads untouched for longer than the retention window.

    Returns the number of threads deleted. Runs as the runtime role, which has
    DELETE on these tables; a thread is one patient's conversation with one
    clinic, and deleting it removes its checkpoints, blobs and pending writes.
    """
    cutoff_days = settings.checkpoint_retention_days
    async with (
        await AsyncConnection.connect(checkpoint_conninfo(settings, admin=False)) as conn,
        conn.transaction(),
    ):
        expired = await conn.execute(
            """
            SELECT thread_id FROM checkpoints
            GROUP BY thread_id
            HAVING max(created_at) < now() - make_interval(days => %s)
            """,
            (cutoff_days,),
        )
        thread_ids = [row[0] for row in await expired.fetchall()]
        if not thread_ids:
            return 0
        for table in _THREAD_TABLES:
            await conn.execute(
                sql.SQL("DELETE FROM {} WHERE thread_id = ANY(%s)").format(sql.Identifier(table)),
                (thread_ids,),
            )
    return len(thread_ids)


def main() -> None:
    """Run one retention sweep. Replaced by a scheduled job in M2."""
    settings = get_settings()
    configure_event_loop_policy()
    deleted = asyncio.run(sweep_expired_threads(settings))
    print(
        f"Deleted {deleted} conversation thread(s) idle for more than "
        f"{settings.checkpoint_retention_days} days."
    )


if __name__ == "__main__":
    main()
