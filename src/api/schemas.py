"""Response models for the review API."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time

from pydantic import BaseModel


class Page[T](BaseModel):
    total: int
    limit: int
    offset: int
    items: list[T]


class Summary(BaseModel):
    app_env: str
    real_patient_data_allowed: bool
    counts: dict[str, int]
    appointments_by_status: dict[str, int]


class ClinicOut(BaseModel):
    id: uuid.UUID
    name: str
    timezone: str
    locale: str
    created_at: datetime


class LocationOut(BaseModel):
    id: uuid.UUID
    clinic_id: uuid.UUID
    name: str
    address: str


class AppointmentTypeOut(BaseModel):
    id: uuid.UUID
    clinic_id: uuid.UUID
    name: str
    duration_minutes: int
    buffer_minutes: int
    sensitivity: str


class AvailabilityRuleOut(BaseModel):
    id: uuid.UUID
    location_id: uuid.UUID
    weekday: int
    weekday_name: str
    start_time: time
    end_time: time
    valid_from: date
    valid_until: date | None


class AvailabilityExceptionOut(BaseModel):
    id: uuid.UUID
    on_date: date
    kind: str
    start_time: time | None
    end_time: time | None


class DoctorOut(BaseModel):
    id: uuid.UUID
    clinic_id: uuid.UUID
    full_name: str
    specialty: str
    active: bool


class DoctorDetail(DoctorOut):
    availability_rules: list[AvailabilityRuleOut]
    availability_exceptions: list[AvailabilityExceptionOut]
    upcoming_active_appointments: int


class PatientOut(BaseModel):
    """Direct identifiers are masked; names are shown so records are reviewable."""

    id: uuid.UUID
    clinic_id: uuid.UUID
    document_type: str
    document_number_masked: str
    given_names: str
    family_names: str
    birth_date: date | None
    age: int | None
    is_minor: bool | None
    phone_masked: str | None
    email_masked: str | None
    created_at: datetime


class ConsentOut(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    purpose: str
    channel: str | None
    granted_at: datetime
    revoked_at: datetime | None
    active: bool
    evidence_kind: str
    policy_version: str


class PhoneBindingOut(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    relationship_kind: str
    verified_at: datetime | None
    revoked_at: datetime | None


class AppointmentOut(BaseModel):
    id: uuid.UUID
    clinic_id: uuid.UUID
    status: str
    start: datetime
    end: datetime
    duration_minutes: int
    expires_at: datetime | None
    doctor_id: uuid.UUID
    doctor_name: str
    specialty: str
    patient_id: uuid.UUID
    patient_name: str
    location_id: uuid.UUID
    location_name: str
    appointment_type: str


class PatientDetail(PatientOut):
    consents: list[ConsentOut]
    phone_bindings: list[PhoneBindingOut]
    appointments: list[AppointmentOut]


class SharedPhonePatient(BaseModel):
    patient_id: uuid.UUID
    patient_name: str
    relationship_kind: str


class SharedPhoneGroup(BaseModel):
    """One handset serving several patients (ADR-18). The number itself is never returned."""

    phone_ref: str
    patient_count: int
    patients: list[SharedPhonePatient]


class AuditEntryOut(BaseModel):
    id: int
    occurred_at: datetime
    actor_kind: str
    actor_id: str | None
    patient_id: uuid.UUID | None
    action: str
    resource: str
    resource_id: str | None
    purpose: str
    channel: str | None
    request_id: str | None
    processor: str | None
    processor_model: str | None
    fields_disclosed: list[str] | None
    zero_retention: bool | None
    row_hash: str | None


class ChainVerification(BaseModel):
    rows_checked: int
    intact: bool
    first_broken_row_id: int | None
