"""Row-level security between clinics (ADR-25).

These tests must fail if a policy is dropped or if a table is added to `app`
without one. They connect as the runtime role, which is how the application
connects. The owner account owns these tables and so is not subject to the
policies; it is used only by migrations and by tests that inspect storage.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.tenancy import ClinicScope, apply_clinic_scope
from src.registry.models import Doctor, Patient
from src.scheduling.models import Appointment
from tests.integration.factories import add_patient_records, create_graph

# Every table a clinic policy must cover. `clinics` is covered separately: it is
# readable by all, since a request must list clinics before it can scope to one.
SCOPED_TABLES = (
    "appointment_types",
    "appointments",
    "availability_exceptions",
    "availability_rules",
    "consents",
    "doctors",
    "locations",
    "patients",
    "phone_bindings",
)


async def _count(session: AsyncSession, model: type) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


@pytest.mark.parametrize("table", SCOPED_TABLES)
async def test_every_clinic_table_has_row_level_security(session: AsyncSession, table: str) -> None:
    enabled = await session.scalar(
        text("SELECT relrowsecurity FROM pg_class WHERE oid = ('app.' || :table)::regclass"),
        {"table": table},
    )
    assert enabled, f"app.{table} does not have row-level security enabled"

    policies = await session.scalar(
        text("SELECT count(*) FROM pg_policies WHERE schemaname = 'app' AND tablename = :table"),
        {"table": table},
    )
    assert policies, f"app.{table} has no policy"


async def test_without_a_scope_nothing_is_visible(session: AsyncSession) -> None:
    """Deny by default: a query that forgot to scope returns nothing, not everything."""
    graph = await create_graph(session)
    await add_patient_records(session, graph)
    assert await _count(session, Patient) == 2

    await session.commit()  # ends the transaction, and with it the scope
    assert await _count(session, Patient) == 0
    assert await _count(session, Appointment) == 0
    assert await _count(session, Doctor) == 0


async def test_one_clinic_cannot_read_another(session: AsyncSession) -> None:
    first = await create_graph(session, name="Clínica Uno")
    await session.commit()
    second = await create_graph(session, name="Clínica Dos")
    await session.commit()

    for scope, visible in ((first, first.patient_id), (second, second.patient_id)):
        await apply_clinic_scope(session, ClinicScope(clinic_id=scope.clinic_id))
        ids = set((await session.scalars(select(Patient.id))).all())
        assert ids == {visible}
        await session.rollback()


async def test_a_write_outside_the_scope_is_rejected(session: AsyncSession) -> None:
    """The WITH CHECK half of the policy: a row cannot be filed under another clinic."""
    first = await create_graph(session, name="Clínica Uno")
    await session.commit()
    second = await create_graph(session, name="Clínica Dos")
    await session.commit()

    await apply_clinic_scope(session, ClinicScope(clinic_id=second.clinic_id))
    session.add(
        Doctor(clinic_id=first.clinic_id, full_name="Dr. Intruso", specialty="Medicina General")
    )
    with pytest.raises(ProgrammingError, match="row-level security policy"):
        await session.flush()


async def test_an_unknown_clinic_scope_sees_nothing(session: AsyncSession) -> None:
    graph = await create_graph(session)
    await add_patient_records(session, graph)
    await session.commit()

    await apply_clinic_scope(session, ClinicScope(clinic_id=uuid.uuid4()))
    assert await _count(session, Patient) == 0


async def test_scope_does_not_survive_the_transaction(session: AsyncSession) -> None:
    """`set_config(..., true)` is transaction-local, so a pooled connection cannot
    carry one request's clinic into the next."""
    graph = await create_graph(session)
    session.add(Doctor(clinic_id=graph.clinic_id, full_name="Dr. Dos", specialty="Pediatría"))
    await session.flush()
    await session.rollback()

    assert await _count(session, Doctor) == 0
