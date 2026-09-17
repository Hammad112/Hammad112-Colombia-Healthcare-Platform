"""The exclusion constraint is the schema's central safety property (ADR-16/17).

These tests fail if someone drops or weakens the constraint, which is exactly
what makes them worth keeping. They require a reachable PostgreSQL instance.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.crypto import blind_index
from src.models import (
    Appointment,
    AppointmentType,
    Clinic,
    Doctor,
    Location,
    Patient,
)
from tests.conftest import requires_db

BOGOTA = timezone(timedelta(hours=-5))


async def _fixture_graph(session: AsyncSession) -> dict[str, uuid.UUID]:
    clinic = Clinic(id=uuid.uuid4(), name="Clínica Prueba")
    # Flush the parent first: these models carry foreign keys without ORM
    # relationships, so the unit of work cannot infer the insert order.
    session.add(clinic)
    await session.flush()

    doctor = Doctor(
        id=uuid.uuid4(), clinic_id=clinic.id, full_name="Dra. Prueba", specialty="Medicina General"
    )
    location = Location(id=uuid.uuid4(), clinic_id=clinic.id, name="Sede", address="Calle 1 # 2-3")
    appt_type = AppointmentType(
        id=uuid.uuid4(), clinic_id=clinic.id, name="Consulta", duration_minutes=20
    )
    patient = Patient(
        id=uuid.uuid4(),
        clinic_id=clinic.id,
        document_type="CC",
        document_number="1020304050",
        document_number_bidx=blind_index("1020304050"),
        given_names="Ana",
        family_names="Gómez Ruiz",
        phone_e164="+573001112233",
        phone_e164_bidx=blind_index("+573001112233"),
    )
    session.add_all([doctor, location, appt_type, patient])
    await session.flush()
    return {
        "clinic": clinic.id,
        "doctor": doctor.id,
        "location": location.id,
        "type": appt_type.id,
        "patient": patient.id,
    }


def _appointment(
    ids: dict[str, uuid.UUID], start: datetime, minutes: int, status: str, **kw: object
) -> Appointment:
    end = start + timedelta(minutes=minutes)
    return Appointment(
        id=uuid.uuid4(),
        clinic_id=ids["clinic"],
        patient_id=ids["patient"],
        doctor_id=ids["doctor"],
        location_id=ids["location"],
        appointment_type_id=ids["type"],
        during=f"[{start.isoformat()},{end.isoformat()})",
        status=status,
        **kw,
    )


@requires_db
async def test_overlapping_appointments_are_refused_by_the_database(
    session: AsyncSession,
) -> None:
    ids = await _fixture_graph(session)
    start = datetime(2026, 10, 1, 9, 0, tzinfo=BOGOTA)

    session.add(_appointment(ids, start, 20, "scheduled"))
    await session.flush()

    # Same doctor, overlapping window: must be rejected.
    session.add(_appointment(ids, start + timedelta(minutes=10), 20, "scheduled"))
    with pytest.raises(IntegrityError):
        await session.flush()


@requires_db
async def test_adjacent_appointments_are_allowed(session: AsyncSession) -> None:
    ids = await _fixture_graph(session)
    start = datetime(2026, 10, 1, 9, 0, tzinfo=BOGOTA)

    session.add(_appointment(ids, start, 20, "scheduled"))
    # Ranges are half-open, so 09:20 starts exactly where 09:00-09:20 ended.
    session.add(_appointment(ids, start + timedelta(minutes=20), 20, "scheduled"))
    await session.flush()  # must not raise


@requires_db
async def test_a_hold_blocks_a_booking(session: AsyncSession) -> None:
    """ADR-17: holds and bookings share one overlap domain."""
    ids = await _fixture_graph(session)
    start = datetime(2026, 10, 1, 11, 0, tzinfo=BOGOTA)

    session.add(
        _appointment(
            ids, start, 20, "hold", expires_at=datetime.now(tz=BOGOTA) + timedelta(minutes=15)
        )
    )
    await session.flush()

    session.add(_appointment(ids, start, 20, "scheduled"))
    with pytest.raises(IntegrityError):
        await session.flush()


@requires_db
async def test_cancelled_appointment_frees_the_slot(session: AsyncSession) -> None:
    ids = await _fixture_graph(session)
    start = datetime(2026, 10, 1, 14, 0, tzinfo=BOGOTA)

    session.add(_appointment(ids, start, 20, "cancelled"))
    session.add(_appointment(ids, start, 20, "scheduled"))
    await session.flush()  # cancelled is outside the constraint's WHERE clause


@requires_db
async def test_hold_without_expiry_is_refused(session: AsyncSession) -> None:
    ids = await _fixture_graph(session)
    start = datetime(2026, 10, 2, 9, 0, tzinfo=BOGOTA)
    session.add(_appointment(ids, start, 20, "hold"))  # no expires_at
    with pytest.raises(IntegrityError):
        await session.flush()
