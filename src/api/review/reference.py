"""Review routes for clinic reference data and the data overview.

Clinics, locations, appointment types and doctors are not patient data, so
these routes record no audit entries.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from src.api.dependencies import PageDep, SessionDep, SettingsDep
from src.api.review.schemas import (
    AppointmentTypeOut,
    AvailabilityExceptionOut,
    AvailabilityRuleOut,
    ClinicOut,
    DoctorDetail,
    DoctorOut,
    LocationOut,
    Page,
    Summary,
    to_page,
)
from src.audit.models import AccessLogEntry
from src.core.db import Base, count_rows
from src.identity.models import Consent, PhoneBinding
from src.registry import repository as registry
from src.registry.models import Clinic, Doctor, Location, Patient
from src.scheduling import repository as scheduling
from src.scheduling.models import (
    Appointment,
    AppointmentType,
    AvailabilityException,
    AvailabilityRule,
)

router = APIRouter()

_COUNTED_TABLES: tuple[tuple[str, type[Base]], ...] = (
    ("clinics", Clinic),
    ("locations", Location),
    ("appointment_types", AppointmentType),
    ("doctors", Doctor),
    ("availability_rules", AvailabilityRule),
    ("availability_exceptions", AvailabilityException),
    ("patients", Patient),
    ("phone_bindings", PhoneBinding),
    ("consents", Consent),
    ("appointments", Appointment),
    ("audit_log", AccessLogEntry),
)


@router.get("/summary", response_model=Summary)
async def summary(session: SessionDep, settings: SettingsDep) -> Summary:
    """Row counts per table and appointments per status. Contains no patient data."""
    return Summary(
        app_env=settings.app_env,
        real_patient_data_allowed=settings.allow_real_patient_data,
        row_counts={name: await count_rows(session, model) for name, model in _COUNTED_TABLES},
        appointments_by_status=await scheduling.count_by_status(session),
    )


@router.get("/clinics", response_model=list[ClinicOut])
async def list_clinics(session: SessionDep) -> list[ClinicOut]:
    return [ClinicOut.build(clinic) for clinic in await registry.list_clinics(session)]


@router.get("/locations", response_model=list[LocationOut])
async def list_locations(
    session: SessionDep, clinic_id: uuid.UUID | None = None
) -> list[LocationOut]:
    locations = await registry.list_locations(session, clinic_id=clinic_id)
    return [LocationOut.build(location) for location in locations]


@router.get("/appointment-types", response_model=list[AppointmentTypeOut])
async def list_appointment_types(
    session: SessionDep, clinic_id: uuid.UUID | None = None
) -> list[AppointmentTypeOut]:
    types = await scheduling.list_appointment_types(session, clinic_id=clinic_id)
    return [AppointmentTypeOut.build(appointment_type) for appointment_type in types]


@router.get("/doctors", response_model=Page[DoctorOut])
async def list_doctors(
    session: SessionDep,
    page: PageDep,
    clinic_id: uuid.UUID | None = None,
    specialty: str | None = None,
) -> Page[DoctorOut]:
    """Doctors ordered by specialty, then name. `specialty` is a case-insensitive substring."""
    result = await registry.list_doctors(
        session, page=page, clinic_id=clinic_id, specialty=specialty
    )
    return to_page(result, [DoctorOut.build(doctor) for doctor in result.items])


@router.get("/doctors/{doctor_id}", response_model=DoctorDetail)
async def get_doctor(session: SessionDep, doctor_id: uuid.UUID) -> DoctorDetail:
    doctor = await registry.get_doctor(session, doctor_id)
    if doctor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Doctor not found")
    rules, exceptions = await scheduling.get_availability(session, doctor_id)
    return DoctorDetail(
        **DoctorOut.build(doctor).model_dump(),
        availability_rules=[AvailabilityRuleOut.build(rule) for rule in rules],
        availability_exceptions=[AvailabilityExceptionOut.build(e) for e in exceptions],
        upcoming_active_appointments=await scheduling.count_upcoming_active(session, doctor_id),
    )
