"""Review routes for clinic reference data and the data overview.

Clinics, locations, appointment types and doctors are not patient data, so these
routes record no audit entries. They are still clinic-scoped: apart from the
clinic list itself, they serve one clinic at a time.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from src.api.dependencies import ClinicScopeDep, PageDep, SessionDep, SettingsDep, require_found
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
from src.core.db import count_rows
from src.identity.models import Consent, PhoneBinding
from src.registry import repository as registry
from src.registry.models import Doctor, Location, Patient
from src.scheduling import repository as scheduling
from src.scheduling.models import (
    Appointment,
    AppointmentType,
    AvailabilityException,
    AvailabilityRule,
)

router = APIRouter()

# Tables counted per clinic in the summary. Row-level security limits each count
# to the clinic in scope.
_CLINIC_TABLES = (
    ("locations", Location),
    ("appointment_types", AppointmentType),
    ("doctors", Doctor),
    ("availability_rules", AvailabilityRule),
    ("availability_exceptions", AvailabilityException),
    ("patients", Patient),
    ("phone_bindings", PhoneBinding),
    ("consents", Consent),
    ("appointments", Appointment),
)


@router.get("/summary", response_model=Summary)
async def summary(session: SessionDep, settings: SettingsDep, scope: ClinicScopeDep) -> Summary:
    """Row counts for the clinic in scope, and appointments per status.

    `audit_log` is counted across all clinics: the audit log records actions
    that have no clinic, and is not subject to the clinic policies.
    """
    counts = {name: await count_rows(session, model) for name, model in _CLINIC_TABLES}
    counts["audit_log"] = await count_rows(session, AccessLogEntry)
    return Summary(
        clinic_id=scope.clinic_id,
        app_env=settings.app_env,
        real_patient_data_allowed=settings.allow_real_patient_data,
        row_counts=counts,
        appointments_by_status=await scheduling.count_by_status(session, clinic_id=scope.clinic_id),
    )


@router.get("/clinics", response_model=list[ClinicOut])
async def list_clinics(session: SessionDep) -> list[ClinicOut]:
    """Every clinic. Use one of these ids as `clinic_id` on the other routes."""
    return [ClinicOut.build(clinic) for clinic in await registry.list_clinics(session)]


@router.get("/locations", response_model=list[LocationOut])
async def list_locations(session: SessionDep, scope: ClinicScopeDep) -> list[LocationOut]:
    locations = await registry.list_locations(session, clinic_id=scope.clinic_id)
    return [LocationOut.build(location) for location in locations]


@router.get("/appointment-types", response_model=list[AppointmentTypeOut])
async def list_appointment_types(
    session: SessionDep, scope: ClinicScopeDep
) -> list[AppointmentTypeOut]:
    types = await scheduling.list_appointment_types(session, clinic_id=scope.clinic_id)
    return [AppointmentTypeOut.build(appointment_type) for appointment_type in types]


@router.get("/doctors", response_model=Page[DoctorOut])
async def list_doctors(
    session: SessionDep,
    scope: ClinicScopeDep,
    page: PageDep,
    specialty: str | None = None,
) -> Page[DoctorOut]:
    """Doctors ordered by specialty, then name. `specialty` is a case-insensitive substring."""
    result = await registry.list_doctors(
        session, clinic_id=scope.clinic_id, page=page, specialty=specialty
    )
    return to_page(result, [DoctorOut.build(doctor) for doctor in result.items])


@router.get("/doctors/{doctor_id}", response_model=DoctorDetail)
async def get_doctor(
    session: SessionDep, scope: ClinicScopeDep, doctor_id: uuid.UUID
) -> DoctorDetail:
    doctor = require_found(
        await registry.get_doctor(session, clinic_id=scope.clinic_id, doctor_id=doctor_id), "Doctor"
    )
    rules, exceptions = await scheduling.get_availability(
        session, clinic_id=scope.clinic_id, doctor_id=doctor_id
    )
    return DoctorDetail(
        **DoctorOut.build(doctor).model_dump(),
        availability_rules=[AvailabilityRuleOut.build(rule) for rule in rules],
        availability_exceptions=[AvailabilityExceptionOut.build(e) for e in exceptions],
        upcoming_active_appointments=await scheduling.count_upcoming_active(
            session, clinic_id=scope.clinic_id, doctor_id=doctor_id
        ),
    )
