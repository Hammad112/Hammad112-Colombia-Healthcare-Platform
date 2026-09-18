"""Queries for appointment types, availability and appointments.

Every function takes the clinic explicitly; row-level security enforces the same
boundary in the database. Appointment results include the patient's name, so
every function returning appointments records one audit entry per appointment
returned, and none of them returns appointments of a soft-deleted patient.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.context import AccessAction
from src.audit.service import Access, record_access, record_accesses
from src.core.pagination import PageRequest, PageResult, fetch_page
from src.registry.models import Doctor, Location, Patient
from src.scheduling.models import (
    ACTIVE_STATUSES,
    Appointment,
    AppointmentStatus,
    AppointmentType,
    AvailabilityException,
    AvailabilityRule,
)


@dataclass(frozen=True, slots=True)
class AppointmentView:
    """An appointment with the display names of the records it references."""

    appointment: Appointment
    doctor_name: str
    specialty: str
    location_name: str
    appointment_type_name: str
    patient_given_names: str
    patient_family_names: str


@dataclass(frozen=True, slots=True)
class AppointmentFilter:
    """Which appointments to return. The clinic is required; the rest narrow further."""

    clinic_id: uuid.UUID
    doctor_id: uuid.UUID | None = None
    patient_id: uuid.UUID | None = None
    status: AppointmentStatus | None = None
    starts_from: datetime | None = None  # inclusive
    starts_before: datetime | None = None  # exclusive


def _appointment_view_statement(clinic_id: uuid.UUID) -> Select[Any]:
    return (
        select(
            Appointment,
            Doctor.full_name,
            Doctor.specialty,
            Location.name,
            AppointmentType.name,
            Patient.given_names,
            Patient.family_names,
        )
        .join(Doctor, Doctor.id == Appointment.doctor_id)
        .join(Location, Location.id == Appointment.location_id)
        .join(AppointmentType, AppointmentType.id == Appointment.appointment_type_id)
        .join(Patient, Patient.id == Appointment.patient_id)
        .where(Appointment.clinic_id == clinic_id, Patient.deleted_at.is_(None))
        .order_by(func.lower(Appointment.during), Appointment.id)
    )


def _to_view(row: Any) -> AppointmentView:
    appointment, doctor_name, specialty, location_name, type_name, given, family = row
    return AppointmentView(
        appointment=appointment,
        doctor_name=doctor_name,
        specialty=specialty,
        location_name=location_name,
        appointment_type_name=type_name,
        patient_given_names=given,
        patient_family_names=family,
    )


async def _record_views(session: AsyncSession, views: list[AppointmentView]) -> None:
    await record_accesses(
        session,
        AccessAction.READ,
        [
            Access(
                resource="appointments",
                resource_id=str(view.appointment.id),
                patient_id=view.appointment.patient_id,
            )
            for view in views
        ],
    )


async def list_appointment_types(
    session: AsyncSession, *, clinic_id: uuid.UUID
) -> list[AppointmentType]:
    statement = (
        select(AppointmentType)
        .where(AppointmentType.clinic_id == clinic_id)
        .order_by(AppointmentType.name)
    )
    return list((await session.scalars(statement)).all())


async def get_availability(
    session: AsyncSession, *, clinic_id: uuid.UUID, doctor_id: uuid.UUID
) -> tuple[list[AvailabilityRule], list[AvailabilityException]]:
    rules = await session.scalars(
        select(AvailabilityRule)
        .where(AvailabilityRule.clinic_id == clinic_id, AvailabilityRule.doctor_id == doctor_id)
        .order_by(AvailabilityRule.weekday, AvailabilityRule.start_time)
    )
    exceptions = await session.scalars(
        select(AvailabilityException)
        .where(
            AvailabilityException.clinic_id == clinic_id,
            AvailabilityException.doctor_id == doctor_id,
        )
        .order_by(AvailabilityException.on_date, AvailabilityException.start_time)
    )
    return list(rules.all()), list(exceptions.all())


async def count_upcoming_active(
    session: AsyncSession, *, clinic_id: uuid.UUID, doctor_id: uuid.UUID
) -> int:
    """Active appointments or holds for the doctor that start now or later."""
    count = await session.scalar(
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.clinic_id == clinic_id,
            Appointment.doctor_id == doctor_id,
            Appointment.status.in_(ACTIVE_STATUSES),
            func.lower(Appointment.during) >= func.now(),
        )
    )
    return int(count or 0)


async def count_by_status(session: AsyncSession, *, clinic_id: uuid.UUID) -> dict[str, int]:
    rows = await session.execute(
        select(Appointment.status, func.count())
        .where(Appointment.clinic_id == clinic_id)
        .group_by(Appointment.status)
    )
    return {str(status): int(count) for status, count in rows.tuples()}


async def list_appointments(
    session: AsyncSession, *, page: PageRequest, filters: AppointmentFilter
) -> PageResult[AppointmentView]:
    """Appointments ordered by start time, then id."""
    statement = _appointment_view_statement(filters.clinic_id)
    if filters.doctor_id is not None:
        statement = statement.where(Appointment.doctor_id == filters.doctor_id)
    if filters.patient_id is not None:
        statement = statement.where(Appointment.patient_id == filters.patient_id)
    if filters.status is not None:
        statement = statement.where(Appointment.status == filters.status)
    if filters.starts_from is not None:
        statement = statement.where(func.lower(Appointment.during) >= filters.starts_from)
    if filters.starts_before is not None:
        statement = statement.where(func.lower(Appointment.during) < filters.starts_before)

    rows, total = await fetch_page(session, statement, page)
    views = [_to_view(row) for row in rows]
    await _record_views(session, views)
    return PageResult(items=views, total=total, limit=page.limit, offset=page.offset)


async def get_appointment(
    session: AsyncSession, *, clinic_id: uuid.UUID, appointment_id: uuid.UUID
) -> AppointmentView | None:
    row = (
        await session.execute(
            _appointment_view_statement(clinic_id).where(Appointment.id == appointment_id)
        )
    ).first()
    if row is None:
        return None
    view = _to_view(row)
    await record_access(
        session,
        AccessAction.READ,
        Access(
            resource="appointments",
            resource_id=str(view.appointment.id),
            patient_id=view.appointment.patient_id,
        ),
    )
    return view


async def list_patient_appointments(
    session: AsyncSession, *, clinic_id: uuid.UUID, patient_id: uuid.UUID
) -> list[AppointmentView]:
    rows = await session.execute(
        _appointment_view_statement(clinic_id).where(Appointment.patient_id == patient_id)
    )
    views = [_to_view(row) for row in rows.all()]
    await _record_views(session, views)
    return views
