"""Queries for clinics, locations, doctors and patients.

Every function that reads clinic data takes the clinic explicitly. Row-level
security enforces the same boundary in the database (see src/core/tenancy.py),
so a mistake here returns nothing rather than another clinic's data.

Functions that return patient rows record one audit entry per patient returned,
in the caller's transaction.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.context import AccessAction
from src.audit.service import Access, record_access, record_accesses
from src.core.crypto import blind_index
from src.core.pagination import PageRequest, PageResult, fetch_page
from src.core.text import LIKE_ESCAPE, contains_pattern
from src.registry.models import Clinic, Doctor, Location, Patient


async def list_clinics(session: AsyncSession) -> list[Clinic]:
    """All clinics. Not clinic data, and used to choose a clinic to work in."""
    return list((await session.scalars(select(Clinic).order_by(Clinic.name))).all())


async def get_clinic(session: AsyncSession, clinic_id: uuid.UUID) -> Clinic | None:
    return await session.get(Clinic, clinic_id)


async def list_locations(session: AsyncSession, *, clinic_id: uuid.UUID) -> list[Location]:
    statement = select(Location).where(Location.clinic_id == clinic_id).order_by(Location.name)
    return list((await session.scalars(statement)).all())


async def list_doctors(
    session: AsyncSession,
    *,
    clinic_id: uuid.UUID,
    page: PageRequest,
    specialty: str | None = None,
) -> PageResult[Doctor]:
    statement = (
        select(Doctor)
        .where(Doctor.clinic_id == clinic_id)
        .order_by(Doctor.specialty, Doctor.full_name, Doctor.id)
    )
    if specialty:
        statement = statement.where(
            Doctor.specialty.ilike(contains_pattern(specialty), escape=LIKE_ESCAPE)
        )
    rows, total = await fetch_page(session, statement, page)
    return PageResult(
        items=[row[0] for row in rows], total=total, limit=page.limit, offset=page.offset
    )


async def get_doctor(
    session: AsyncSession, *, clinic_id: uuid.UUID, doctor_id: uuid.UUID
) -> Doctor | None:
    doctor: Doctor | None = await session.scalar(
        select(Doctor).where(Doctor.id == doctor_id, Doctor.clinic_id == clinic_id)
    )
    return doctor


async def list_patients(
    session: AsyncSession,
    *,
    clinic_id: uuid.UUID,
    page: PageRequest,
    phone_e164: str | None = None,
    document_number: str | None = None,
) -> PageResult[Patient]:
    """Active patients of the clinic, oldest first.

    Names and identifiers are encrypted, so there is no text search. `phone_e164`
    and `document_number` are exact matches through the blind indexes.
    """
    statement = (
        select(Patient)
        .where(Patient.clinic_id == clinic_id, Patient.deleted_at.is_(None))
        .order_by(Patient.created_at, Patient.id)
    )
    if phone_e164:
        statement = statement.where(Patient.phone_e164_bidx == blind_index(phone_e164))
    if document_number:
        statement = statement.where(Patient.document_number_bidx == blind_index(document_number))

    rows, total = await fetch_page(session, statement, page)
    patients = [row[0] for row in rows]
    await record_accesses(
        session,
        AccessAction.READ,
        [Access(resource="patients", resource_id=str(p.id), patient_id=p.id) for p in patients],
    )
    return PageResult(items=patients, total=total, limit=page.limit, offset=page.offset)


async def get_patient(
    session: AsyncSession, *, clinic_id: uuid.UUID, patient_id: uuid.UUID
) -> Patient | None:
    """An active patient of the clinic. Records the access only when one is returned."""
    patient = await session.scalar(
        select(Patient).where(
            Patient.id == patient_id,
            Patient.clinic_id == clinic_id,
            Patient.deleted_at.is_(None),
        )
    )
    if patient is None:
        return None
    await record_access(
        session,
        AccessAction.READ,
        Access(resource="patients", resource_id=str(patient.id), patient_id=patient.id),
    )
    return patient
