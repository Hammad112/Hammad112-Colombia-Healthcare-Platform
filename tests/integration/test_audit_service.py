"""Hash-chained audit log: recording, tamper detection, concurrency."""

from __future__ import annotations

import asyncio
import uuid

import psycopg
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.audit.context import AccessAction, AuditContext
from src.audit.models import AccessLogEntry
from src.audit.service import Access, record_access, record_accesses, verify_chain
from src.core.config import Settings


async def _record(session: AsyncSession, count: int) -> None:
    await record_accesses(
        session,
        AccessAction.READ,
        [Access(resource="patients", patient_id=uuid.uuid4()) for _ in range(count)],
    )
    await session.commit()


async def test_entries_carry_the_bound_context(
    session: AsyncSession, staff_context: AuditContext
) -> None:
    patient_id = uuid.uuid4()
    await record_access(
        session,
        AccessAction.DISCLOSE,
        Access(
            resource="messages",
            patient_id=patient_id,
            processor="model-provider",
            processor_model="generator-v1",
            fields_disclosed=("given_names", "appointment_start"),
            zero_retention=True,
        ),
    )
    await session.commit()

    entry = (await session.scalars(select(AccessLogEntry))).one()
    assert (entry.actor_kind, entry.actor_id, entry.purpose) == (
        "staff",
        "tester",
        "integration_test",
    )
    assert entry.request_id == staff_context.request_id
    assert entry.patient_id == patient_id
    assert entry.fields_disclosed == ["given_names", "appointment_start"]


async def test_recording_without_context_raises(session: AsyncSession) -> None:
    with pytest.raises(RuntimeError, match="AuditContext"):
        await record_access(session, AccessAction.READ, Access(resource="patients"))


async def test_chain_verifies_intact(session: AsyncSession, staff_context: AuditContext) -> None:
    await _record(session, 5)
    result = await verify_chain(session)
    assert (result.rows_checked, result.intact) == (5, True)


async def test_altered_entry_is_detected(
    session: AsyncSession, staff_context: AuditContext, owner_connection: psycopg.Connection
) -> None:
    await _record(session, 5)
    # Simulate an attacker with owner rights who disables the append-only trigger.
    owner_connection.execute("ALTER TABLE audit.access_log DISABLE TRIGGER access_log_append_only")
    try:
        owner_connection.execute("UPDATE audit.access_log SET purpose = 'altered' WHERE id = 3")
    finally:
        owner_connection.execute(
            "ALTER TABLE audit.access_log ENABLE TRIGGER access_log_append_only"
        )

    result = await verify_chain(session)
    assert result.first_broken_id == 3


async def test_deleted_entry_is_detected(
    session: AsyncSession, staff_context: AuditContext, owner_connection: psycopg.Connection
) -> None:
    await _record(session, 5)
    owner_connection.execute("ALTER TABLE audit.access_log DISABLE TRIGGER access_log_append_only")
    try:
        owner_connection.execute("DELETE FROM audit.access_log WHERE id = 2")
    finally:
        owner_connection.execute(
            "ALTER TABLE audit.access_log ENABLE TRIGGER access_log_append_only"
        )

    result = await verify_chain(session)
    assert result.first_broken_id == 3  # the entry after the gap no longer links


async def test_concurrent_writers_keep_one_unbroken_chain(
    staff_context: AuditContext, settings: Settings, session: AsyncSession
) -> None:
    engine = create_async_engine(settings.app_database_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def writer() -> None:
        async with maker() as writer_session:
            await _record(writer_session, 4)

    try:
        await asyncio.gather(*(writer() for _ in range(6)))
    finally:
        await engine.dispose()

    result = await verify_chain(session)
    assert (result.rows_checked, result.intact) == (24, True)
