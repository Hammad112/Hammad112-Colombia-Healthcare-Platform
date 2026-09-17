"""Review routes for patients and appointments. Every read is audited by the repositories."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from src.api.dependencies import PageDep, SessionDep
from src.api.review.schemas import (
    AppointmentOut,
    ConsentOut,
    Page,
    PatientDetail,
    PatientOut,
    PhoneBindingOut,
    to_page,
)
from src.core.timezones import BOGOTA
from src.identity import repository as identity
from src.registry import repository as registry
from src.scheduling import repository as scheduling
from src.scheduling.models import AppointmentStatus

router = APIRouter()


def _today() -> date:
    return datetime.now(BOGOTA).date()


@router.get("/patients", response_model=Page[PatientOut])
async def list_patients(
    session: SessionDep,
    page: PageDep,
    clinic_id: uuid.UUID | None = None,
    phone: Annotated[
        str | None, Query(description="Exact E.164 number, for example +573001112233")
    ] = None,
    document_number: Annotated[str | None, Query(description="Exact document number")] = None,
) -> Page[PatientOut]:
    """Active patients, oldest first.

    Names and identifiers are encrypted at rest, so there is no name search.
    `phone` and `document_number` match exactly through the blind indexes.
    """
    result = await registry.list_patients(
        session, page=page, clinic_id=clinic_id, phone_e164=phone, document_number=document_number
    )
    today = _today()
    return to_page(result, [PatientOut.build(p, today=today) for p in result.items])


@router.get("/patients/{patient_id}", response_model=PatientDetail)
async def get_patient(session: SessionDep, patient_id: uuid.UUID) -> PatientDetail:
    """One patient with their consents, phone bindings and appointments."""
    patient = await registry.get_patient(session, patient_id)
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    consents = await identity.list_patient_consents(session, patient_id)
    bindings = await identity.list_patient_bindings(session, patient_id)
    appointments = await scheduling.list_patient_appointments(session, patient_id)
    return PatientDetail(
        **PatientOut.build(patient, today=_today()).model_dump(),
        consents=[ConsentOut.build(consent) for consent in consents],
        phone_bindings=[PhoneBindingOut.build(binding) for binding in bindings],
        appointments=[AppointmentOut.build(view) for view in appointments],
    )


@router.get("/appointments", response_model=Page[AppointmentOut])
async def list_appointments(
    session: SessionDep,
    page: PageDep,
    clinic_id: uuid.UUID | None = None,
    doctor_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    status_filter: Annotated[AppointmentStatus | None, Query(alias="status")] = None,
    date_from: Annotated[
        date | None, Query(description="First Bogotá calendar day to include")
    ] = None,
    date_to: Annotated[
        date | None, Query(description="Last Bogotá calendar day to include")
    ] = None,
) -> Page[AppointmentOut]:
    """Appointments ordered by start time. Date filters apply to the start time."""
    filters = scheduling.AppointmentFilter(
        clinic_id=clinic_id,
        doctor_id=doctor_id,
        patient_id=patient_id,
        status=status_filter,
        starts_from=datetime.combine(date_from, time.min, tzinfo=BOGOTA) if date_from else None,
        starts_before=(
            datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=BOGOTA)
            if date_to
            else None
        ),
    )
    result = await scheduling.list_appointments(session, page=page, filters=filters)
    return to_page(result, [AppointmentOut.build(view) for view in result.items])


@router.get("/appointments/{appointment_id}", response_model=AppointmentOut)
async def get_appointment(session: SessionDep, appointment_id: uuid.UUID) -> AppointmentOut:
    view = await scheduling.get_appointment(session, appointment_id)
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
    return AppointmentOut.build(view)
