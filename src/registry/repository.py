"""Queries for clinics, locations, doctors and patients.

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
    return list((await session.scalars(select(Clinic).order_by(Clinic.name))).all())


async def list_locations(session: AsyncSession, *, clinic_id: uuid.UUID | None) -> list[Location]:
    statement = select(Location).order_by(Location.name)
    if clinic_id is not None:
        statement = statement.where(Location.clinic_id == clinic_id)
    return list((await session.scalars(statement)).all())


async def list_doctors(
    session: AsyncSession,
    *,
    page: PageRequest,
    clinic_id: uuid.UUID | None = None,
    specialty: str | None = None,
) -> PageResult[Doctor]:
    statement = select(Doctor).order_by(Doctor.specialty, Doctor.full_name, Doctor.id)
    if clinic_id is not None:
        statement = statement.where(Doctor.clinic_id == clinic_id)
    if specialty:
        statement = statement.where(
            Doctor.specialty.ilike(contains_pattern(specialty), escape=LIKE_ESCAPE)
        )
    rows, total = await fetch_page(session, statement, page)
    return PageResult(
        items=[row[0] for row in rows], total=total, limit=page.limit, offset=page.offset
    )


async def get_doctor(session: AsyncSession, doctor_id: uuid.UUID) -> Doctor | None:
    return await session.get(Doctor, doctor_id)


async def list_patients(
    session: AsyncSession,
    *,
    page: PageRequest,
    clinic_id: uuid.UUID | None = None,
    phone_e164: str | None = None,
    document_number: str | None = None,
) -> PageResult[Patient]:
    """Active patients, oldest first.

    Names and identifiers are encrypted, so there is no text search. `phone_e164`
    and `document_number` are exact matches through the blind indexes.
    """
    statement = (
        select(Patient).where(Patient.deleted_at.is_(None)).order_by(Patient.created_at, Patient.id)
    )
    if clinic_id is not None:
        statement = statement.where(Patient.clinic_id == clinic_id)
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


async def get_patient(session: AsyncSession, patient_id: uuid.UUID) -> Patient | None:
    """An active patient by id. Records the access only when a patient is returned."""
    patient = await session.get(Patient, patient_id)
    if patient is None or patient.deleted_at is not None:
        return None
    await record_access(
        session,
        AccessAction.READ,
        Access(resource="patients", resource_id=str(patient.id), patient_id=patient.id),
    )
    return patient
