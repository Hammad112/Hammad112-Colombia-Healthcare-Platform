"""Read-only review API over the (synthetic) data.

Exists so the seeded data can be inspected and the M0 safety properties can be
seen working: blind-index lookup of encrypted fields, shared-handset bindings,
consent records, the exclusion-constrained appointments, and the audit chain.

Guardrails, because staff authentication does not exist until M11:
* Available only when APP_ENV is local or ci AND the real-data gate is closed.
  Anywhere else every route returns 404, as if it did not exist.
* Direct identifiers (document number, phone, email) are masked in responses.
* Every read of patient data writes an audit row, and
  tests/test_review_endpoints.py fails if a patient-data route stops doing so.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api import schemas as s
from src.audit import log as audit
from src.audit.context import AuditContext, set_context
from src.core.config import Settings, get_settings
from src.core.crypto import blind_index
from src.core.db import get_session
from src.models import (
    AccessLog,
    Appointment,
    AppointmentType,
    AvailabilityException,
    AvailabilityRule,
    Clinic,
    Consent,
    Doctor,
    Location,
    Patient,
    PhoneBinding,
)

# Colombia is UTC-5 all year with no daylight saving. A fixed offset avoids a
# tzdata dependency on Windows.
BOGOTA = timezone(timedelta(hours=-5))
WEEKDAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


async def review_enabled(request: Request, settings: Settings = Depends(get_settings)) -> None:
    if settings.app_env not in ("local", "ci") or settings.allow_real_patient_data:
        raise HTTPException(status_code=404)
    set_context(
        AuditContext(
            actor_kind="staff",
            actor_id="local-reviewer",
            purpose="synthetic_data_review",
            channel="review_api",
            request_id=getattr(request.state, "request_id", None) or str(uuid.uuid4()),
            ip=request.client.host if request.client else None,
        )
    )


router = APIRouter(
    prefix="/review",
    tags=["review (synthetic data, local only)"],
    dependencies=[Depends(review_enabled)],
)

Limit = Query(50, ge=1, le=200)
Offset = Query(0, ge=0)


# ---------------------------------------------------------------- helpers


def _mask(value: str | None, keep: int = 4) -> str | None:
    if not value:
        return value
    return "*" * max(len(value) - keep, 0) + value[-keep:]


def _mask_email(value: str | None) -> str | None:
    if not value or "@" not in value:
        return _mask(value)
    local, domain = value.split("@", 1)
    return f"{local[:1]}***@{domain}"


def _age(birth: date | None) -> int | None:
    if birth is None:
        return None
    today = datetime.now(tz=BOGOTA).date()
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def _patient_out(p: Patient) -> s.PatientOut:
    age = _age(p.birth_date)
    return s.PatientOut(
        id=p.id,
        clinic_id=p.clinic_id,
        document_type=p.document_type,
        document_number_masked=_mask(p.document_number) or "",
        given_names=p.given_names,
        family_names=p.family_names,
        birth_date=p.birth_date,
        age=age,
        is_minor=None if age is None else age < 18,
        phone_masked=_mask(p.phone_e164),
        email_masked=_mask_email(p.email),
        created_at=p.created_at,
    )


def _consent_out(c: Consent) -> s.ConsentOut:
    return s.ConsentOut(
        id=c.id,
        patient_id=c.patient_id,
        purpose=c.purpose,
        channel=c.channel,
        granted_at=c.granted_at,
        revoked_at=c.revoked_at,
        active=c.revoked_at is None,
        evidence_kind=c.evidence_kind,
        policy_version=c.policy_version,
    )


async def _page(
    session: AsyncSession, stmt: Select[Any], limit: int, offset: int
) -> tuple[int, list[Any]]:
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = (await session.execute(stmt.limit(limit).offset(offset))).all()
    return int(total or 0), list(rows)


def _appointments_stmt() -> Select[Any]:
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
        .order_by(func.lower(Appointment.during), Appointment.id)
    )


def _appointment_out(row: Any) -> s.AppointmentOut:
    appt, doctor_name, specialty, location_name, type_name, given, family = row
    start = appt.during.lower.astimezone(BOGOTA)
    end = appt.during.upper.astimezone(BOGOTA)
    return s.AppointmentOut(
        id=appt.id,
        clinic_id=appt.clinic_id,
        status=appt.status,
        start=start,
        end=end,
        duration_minutes=int((end - start).total_seconds() // 60),
        expires_at=appt.expires_at,
        doctor_id=appt.doctor_id,
        doctor_name=doctor_name,
        specialty=specialty,
        patient_id=appt.patient_id,
        patient_name=f"{given} {family}",
        location_id=appt.location_id,
        location_name=location_name,
        appointment_type=type_name,
    )


async def _audit_patients(
    session: AsyncSession, resource: str, pairs: list[tuple[str, uuid.UUID]]
) -> None:
    for resource_id, patient_id in pairs:
        await audit.emit(
            session,
            action="read",
            resource=resource,
            resource_id=resource_id,
            patient_id=patient_id,
        )


# ---------------------------------------------------------------- overview


@router.get("/summary", response_model=s.Summary)
async def summary(
    session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)
) -> s.Summary:
    """Row counts per table. No patient data, so no audit row."""
    counts: dict[str, int] = {}
    for name, model in (
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
        ("audit_log", AccessLog),
    ):
        counts[name] = int(await session.scalar(select(func.count()).select_from(model)) or 0)
    status_rows = await session.execute(
        select(Appointment.status, func.count()).group_by(Appointment.status)
    )
    by_status: dict[str, int] = {status: int(n) for status, n in status_rows.tuples()}
    return s.Summary(
        app_env=settings.app_env,
        real_patient_data_allowed=settings.allow_real_patient_data,
        counts=counts,
        appointments_by_status=by_status,
    )


# ---------------------------------------------------------------- clinic reference data


@router.get("/clinics", response_model=list[s.ClinicOut])
async def list_clinics(session: AsyncSession = Depends(get_session)) -> list[s.ClinicOut]:
    rows = (await session.scalars(select(Clinic).order_by(Clinic.name))).all()
    return [s.ClinicOut.model_validate(c, from_attributes=True) for c in rows]


@router.get("/locations", response_model=list[s.LocationOut])
async def list_locations(
    clinic_id: uuid.UUID | None = None, session: AsyncSession = Depends(get_session)
) -> list[s.LocationOut]:
    stmt = select(Location).order_by(Location.name)
    if clinic_id:
        stmt = stmt.where(Location.clinic_id == clinic_id)
    return [
        s.LocationOut.model_validate(x, from_attributes=True)
        for x in (await session.scalars(stmt)).all()
    ]


@router.get("/appointment-types", response_model=list[s.AppointmentTypeOut])
async def list_appointment_types(
    clinic_id: uuid.UUID | None = None, session: AsyncSession = Depends(get_session)
) -> list[s.AppointmentTypeOut]:
    stmt = select(AppointmentType).order_by(AppointmentType.name)
    if clinic_id:
        stmt = stmt.where(AppointmentType.clinic_id == clinic_id)
    return [
        s.AppointmentTypeOut.model_validate(x, from_attributes=True)
        for x in (await session.scalars(stmt)).all()
    ]


@router.get("/doctors", response_model=s.Page[s.DoctorOut])
async def list_doctors(
    clinic_id: uuid.UUID | None = None,
    specialty: str | None = None,
    limit: int = Limit,
    offset: int = Offset,
    session: AsyncSession = Depends(get_session),
) -> s.Page[s.DoctorOut]:
    stmt = select(Doctor).order_by(Doctor.specialty, Doctor.full_name)
    if clinic_id:
        stmt = stmt.where(Doctor.clinic_id == clinic_id)
    if specialty:
        stmt = stmt.where(Doctor.specialty.ilike(f"%{specialty}%"))
    total, rows = await _page(session, stmt, limit, offset)
    return s.Page(
        total=total,
        limit=limit,
        offset=offset,
        items=[s.DoctorOut.model_validate(r[0], from_attributes=True) for r in rows],
    )


@router.get("/doctors/{doctor_id}", response_model=s.DoctorDetail)
async def get_doctor(
    doctor_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> s.DoctorDetail:
    doctor = await session.get(Doctor, doctor_id)
    if doctor is None:
        raise HTTPException(status_code=404, detail="Doctor not found")
    rules = (
        await session.scalars(
            select(AvailabilityRule)
            .where(AvailabilityRule.doctor_id == doctor_id)
            .order_by(AvailabilityRule.weekday, AvailabilityRule.start_time)
        )
    ).all()
    exceptions = (
        await session.scalars(
            select(AvailabilityException)
            .where(AvailabilityException.doctor_id == doctor_id)
            .order_by(AvailabilityException.on_date)
        )
    ).all()
    upcoming = await session.scalar(
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.doctor_id == doctor_id,
            Appointment.status.in_(("hold", "scheduled", "confirmed", "checked_in")),
            func.lower(Appointment.during) >= func.now(),
        )
    )
    return s.DoctorDetail(
        **s.DoctorOut.model_validate(doctor, from_attributes=True).model_dump(),
        availability_rules=[
            s.AvailabilityRuleOut(
                id=r.id,
                location_id=r.location_id,
                weekday=r.weekday,
                weekday_name=WEEKDAYS_ES[r.weekday],
                start_time=r.start_time,
                end_time=r.end_time,
                valid_from=r.valid_from,
                valid_until=r.valid_until,
            )
            for r in rules
        ],
        availability_exceptions=[
            s.AvailabilityExceptionOut.model_validate(e, from_attributes=True) for e in exceptions
        ],
        upcoming_active_appointments=int(upcoming or 0),
    )


# ---------------------------------------------------------------- patient data (audited)


@router.get("/patients", response_model=s.Page[s.PatientOut])
async def list_patients(
    clinic_id: uuid.UUID | None = None,
    document_number: str | None = Query(None, description="Exact match, via the blind index"),
    phone: str | None = Query(
        None, description="Exact E.164 match, e.g. +573001112233, via the blind index"
    ),
    limit: int = Limit,
    offset: int = Offset,
    session: AsyncSession = Depends(get_session),
) -> s.Page[s.PatientOut]:
    """Names and identifiers are encrypted at rest, so free-text search is not
    possible. Exact lookup works through the HMAC blind index, the same path an
    inbound WhatsApp webhook will use to find a patient by phone number."""
    stmt = (
        select(Patient).where(Patient.deleted_at.is_(None)).order_by(Patient.created_at, Patient.id)
    )
    if clinic_id:
        stmt = stmt.where(Patient.clinic_id == clinic_id)
    if document_number:
        stmt = stmt.where(Patient.document_number_bidx == blind_index(document_number))
    if phone:
        stmt = stmt.where(Patient.phone_e164_bidx == blind_index(phone))
    total, rows = await _page(session, stmt, limit, offset)
    patients = [r[0] for r in rows]
    await _audit_patients(session, "patients", [(str(p.id), p.id) for p in patients])
    return s.Page(
        total=total, limit=limit, offset=offset, items=[_patient_out(p) for p in patients]
    )


@router.get("/patients/{patient_id}", response_model=s.PatientDetail)
async def get_patient(
    patient_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> s.PatientDetail:
    patient = await session.get(Patient, patient_id)
    if patient is None or patient.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Patient not found")
    consents = (
        await session.scalars(
            select(Consent).where(Consent.patient_id == patient_id).order_by(Consent.granted_at)
        )
    ).all()
    bindings = (
        await session.scalars(select(PhoneBinding).where(PhoneBinding.patient_id == patient_id))
    ).all()
    appts = (
        await session.execute(_appointments_stmt().where(Appointment.patient_id == patient_id))
    ).all()
    await audit.emit(
        session,
        action="read",
        resource="patients",
        resource_id=str(patient_id),
        patient_id=patient_id,
    )
    return s.PatientDetail(
        **_patient_out(patient).model_dump(),
        consents=[_consent_out(c) for c in consents],
        phone_bindings=[
            s.PhoneBindingOut.model_validate(b, from_attributes=True) for b in bindings
        ],
        appointments=[_appointment_out(r) for r in appts],
    )


@router.get("/appointments", response_model=s.Page[s.AppointmentOut])
async def list_appointments(
    clinic_id: uuid.UUID | None = None,
    doctor_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
    status: str | None = Query(
        None, description="hold, scheduled, confirmed, checked_in, completed, no_show, cancelled"
    ),
    date_from: date | None = Query(None, description="Bogotá local date, inclusive"),
    date_to: date | None = Query(None, description="Bogotá local date, inclusive"),
    limit: int = Limit,
    offset: int = Offset,
    session: AsyncSession = Depends(get_session),
) -> s.Page[s.AppointmentOut]:
    stmt = _appointments_stmt()
    if clinic_id:
        stmt = stmt.where(Appointment.clinic_id == clinic_id)
    if doctor_id:
        stmt = stmt.where(Appointment.doctor_id == doctor_id)
    if patient_id:
        stmt = stmt.where(Appointment.patient_id == patient_id)
    if status:
        stmt = stmt.where(Appointment.status == status)
    if date_from:
        start = datetime.combine(date_from, datetime.min.time(), tzinfo=BOGOTA)
        stmt = stmt.where(func.lower(Appointment.during) >= start)
    if date_to:
        end = datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=BOGOTA)
        stmt = stmt.where(func.lower(Appointment.during) < end)
    total, rows = await _page(session, stmt, limit, offset)
    items = [_appointment_out(r) for r in rows]
    await _audit_patients(session, "appointments", [(str(a.id), a.patient_id) for a in items])
    return s.Page(total=total, limit=limit, offset=offset, items=items)


@router.get("/appointments/{appointment_id}", response_model=s.AppointmentOut)
async def get_appointment(
    appointment_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> s.AppointmentOut:
    row = (
        await session.execute(_appointments_stmt().where(Appointment.id == appointment_id))
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Appointment not found")
    item = _appointment_out(row)
    await audit.emit(
        session,
        action="read",
        resource="appointments",
        resource_id=str(item.id),
        patient_id=item.patient_id,
    )
    return item


@router.get("/consents", response_model=s.Page[s.ConsentOut])
async def list_consents(
    patient_id: uuid.UUID | None = None,
    purpose: str | None = None,
    active_only: bool = False,
    limit: int = Limit,
    offset: int = Offset,
    session: AsyncSession = Depends(get_session),
) -> s.Page[s.ConsentOut]:
    stmt = select(Consent).order_by(Consent.granted_at, Consent.id)
    if patient_id:
        stmt = stmt.where(Consent.patient_id == patient_id)
    if purpose:
        stmt = stmt.where(Consent.purpose == purpose)
    if active_only:
        stmt = stmt.where(Consent.revoked_at.is_(None))
    total, rows = await _page(session, stmt, limit, offset)
    consents = [r[0] for r in rows]
    await _audit_patients(session, "consents", [(str(c.id), c.patient_id) for c in consents])
    return s.Page(
        total=total,
        limit=limit,
        offset=offset,
        items=[_consent_out(c) for c in consents],
    )


@router.get("/phone-bindings/shared", response_model=list[s.SharedPhoneGroup])
async def shared_phones(session: AsyncSession = Depends(get_session)) -> list[s.SharedPhoneGroup]:
    """Handsets bound to more than one patient: the household case ADR-18 designs for.
    Returns an opaque reference, never the phone number."""
    shared = (
        select(PhoneBinding.phone_e164_bidx)
        .where(PhoneBinding.revoked_at.is_(None))
        .group_by(PhoneBinding.phone_e164_bidx)
        .having(func.count() > 1)
    )
    rows = (
        await session.execute(
            select(PhoneBinding, Patient.given_names, Patient.family_names)
            .join(Patient, Patient.id == PhoneBinding.patient_id)
            .where(PhoneBinding.revoked_at.is_(None), PhoneBinding.phone_e164_bidx.in_(shared))
            .order_by(PhoneBinding.phone_e164_bidx, PhoneBinding.relationship_kind)
        )
    ).all()
    groups: dict[bytes, list[s.SharedPhonePatient]] = {}
    for binding, given, family in rows:
        groups.setdefault(binding.phone_e164_bidx, []).append(
            s.SharedPhonePatient(
                patient_id=binding.patient_id,
                patient_name=f"{given} {family}",
                relationship_kind=binding.relationship_kind,
            )
        )
    await _audit_patients(
        session,
        "phone_bindings",
        [(str(p.patient_id), p.patient_id) for ps in groups.values() for p in ps],
    )
    return [
        s.SharedPhoneGroup(phone_ref=bidx[:6].hex(), patient_count=len(ps), patients=ps)
        for bidx, ps in groups.items()
    ]


# ---------------------------------------------------------------- audit log


@router.get("/audit-log", response_model=s.Page[s.AuditEntryOut])
async def list_audit_log(
    patient_id: uuid.UUID | None = None,
    action: str | None = None,
    resource: str | None = None,
    limit: int = Limit,
    offset: int = Offset,
    session: AsyncSession = Depends(get_session),
) -> s.Page[s.AuditEntryOut]:
    """Newest first. Reading the audit log is itself recorded."""
    stmt = select(AccessLog).order_by(AccessLog.id.desc())
    if patient_id:
        stmt = stmt.where(AccessLog.patient_id == patient_id)
    if action:
        stmt = stmt.where(AccessLog.action == action)
    if resource:
        stmt = stmt.where(AccessLog.resource == resource)
    total, rows = await _page(session, stmt, limit, offset)
    items = [
        s.AuditEntryOut(
            **{k: getattr(r[0], k) for k in s.AuditEntryOut.model_fields if k != "row_hash"},
            row_hash=r[0].row_hash.hex()[:16] if r[0].row_hash else None,
        )
        for r in rows
    ]
    await audit.emit(session, action="read", resource="audit_log", patient_id=patient_id)
    return s.Page(total=total, limit=limit, offset=offset, items=items)


@router.get("/audit-log/verify", response_model=s.ChainVerification)
async def verify_audit_log(session: AsyncSession = Depends(get_session)) -> s.ChainVerification:
    """Recomputes the hash chain. `intact: false` means rows were altered or removed."""
    checked, broken = await audit.verify_chain(session)
    await audit.emit(session, action="read", resource="audit_log", resource_id="verify")
    return s.ChainVerification(
        rows_checked=checked, intact=broken is None, first_broken_row_id=broken
    )
