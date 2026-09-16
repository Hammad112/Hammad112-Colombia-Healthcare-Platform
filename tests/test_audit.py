"""Audit log behaviour (ADR-09, ADR-24)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit import log as audit
from src.audit.context import AuditContext, set_context
from src.models import AccessLog
from tests.conftest import requires_db


def test_missing_context_raises_rather_than_defaulting() -> None:
    """An unaudited patient-data path is a defect, not something to paper over."""
    from src.audit.context import _ctx, get_context

    token = _ctx.set(None)
    try:
        with pytest.raises(RuntimeError, match="AuditContext"):
            get_context()
    finally:
        _ctx.reset(token)


@requires_db
async def test_emit_writes_a_row_with_request_context(session: AsyncSession) -> None:
    clinic_id, patient_id = uuid.uuid4(), uuid.uuid4()
    set_context(
        AuditContext(
            actor_kind="staff",
            actor_id="recepcion-01",
            clinic_id=clinic_id,
            purpose="scheduling",
            channel="web",
        )
    )
    await audit.emit(session, action="read", resource="patients", patient_id=patient_id)
    await session.flush()

    row = (await session.execute(select(AccessLog))).scalars().one()
    assert row.actor_id == "recepcion-01"
    assert row.purpose == "scheduling"
    assert row.patient_id == patient_id
    assert row.row_hash is not None


@requires_db
async def test_model_disclosure_is_recorded_without_prompt_bodies(
    session: AsyncSession,
) -> None:
    """ADR-24: record which fields went to which processor, never the prompt."""
    set_context(AuditContext(actor_kind="agent", purpose="reminder", clinic_id=uuid.uuid4()))
    await audit.emit(
        session,
        action="disclose",
        resource="messages",
        processor="model-provider",
        processor_model="generator-v1",
        fields_disclosed=["given_names", "appointment_start", "doctor_full_name"],
        zero_retention=True,
    )
    await session.flush()

    row = (await session.execute(select(AccessLog))).scalars().one()
    assert row.fields_disclosed == ["given_names", "appointment_start", "doctor_full_name"]
    assert row.zero_retention is True
    # There is no column to hold a prompt, by design.
    assert not hasattr(row, "prompt")


@requires_db
async def test_rows_are_hash_chained(session: AsyncSession) -> None:
    set_context(AuditContext(actor_kind="system", purpose="test"))
    first = await audit.emit(session, action="read", resource="patients")
    second = await audit.emit(session, action="read", resource="patients")
    await session.flush()
    assert second.prev_hash == first.row_hash


@requires_db
async def test_update_is_refused_by_trigger(session: AsyncSession) -> None:
    """Append-only, mechanism 1. Mechanism 2 (role grants) is verified in deployment."""
    set_context(AuditContext(actor_kind="system", purpose="test"))
    await audit.emit(session, action="read", resource="patients")
    await session.flush()

    await session.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION audit.reject_mutation() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'audit.access_log is append-only (attempted %)', TG_OP;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    await session.execute(
        text(
            """
            DROP TRIGGER IF EXISTS access_log_append_only ON audit.access_log;
            CREATE TRIGGER access_log_append_only
            BEFORE UPDATE OR DELETE ON audit.access_log
            FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();
            """
        )
    )

    with pytest.raises(Exception, match="append-only"):
        await session.execute(text("UPDATE audit.access_log SET purpose = 'tampered'"))
