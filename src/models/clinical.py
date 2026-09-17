"""Core scheduling entities (M0 base schema).

Design notes that matter:

* Availability is modelled as RULES plus EXCEPTIONS, never as materialized slots.
  The scope requires "real availability, not fixed slots"; stored slots drift.
* `appointments` carries a `during` tstzrange and an exclusion constraint, so an
  overlapping booking for the same doctor is refused by PostgreSQL regardless of
  application logic (ADR-16).
* A hold is an appointment row with status 'hold' and an `expires_at`. It shares
  the exclusion constraint with real bookings, so a hold blocks a booking and a
  booking blocks a hold, with one table and one constraint (ADR-17).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Time,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.crypto import EncryptedStr
from src.models.base import Base, Timestamped, UUIDPrimaryKey

# Statuses that occupy a doctor's time. Anything here participates in the
# overlap constraint; anything outside it frees the slot.
ACTIVE_STATUSES = ("hold", "scheduled", "confirmed", "checked_in")

# RIPS document type codes used across the Colombian health system.
DOCUMENT_TYPES = ("CC", "TI", "CE", "PA", "RC", "PE", "PPT", "MS", "AS", "CN")


class Clinic(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "clinics"

    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), server_default=text("'America/Bogota'"))
    locale: Mapped[str] = mapped_column(String(16), server_default=text("'es-CO'"))
    settings: Mapped[dict[str, object]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class Patient(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "patients"
    __table_args__ = (
        CheckConstraint("document_type IN " + str(DOCUMENT_TYPES), name="document_type_valid"),
        Index(
            "ix_patients_clinic_phone_bidx",
            "clinic_id",
            "phone_e164_bidx",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_patients_clinic_document",
            "clinic_id",
            "document_type",
            "document_number_bidx",
            unique=True,
        ),
        {"schema": "app"},
    )

    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.clinics.id"))
    document_type: Mapped[str] = mapped_column(String(4))
    document_number: Mapped[str] = mapped_column(EncryptedStr)
    document_number_bidx: Mapped[bytes] = mapped_column(LargeBinary)
    given_names: Mapped[str] = mapped_column(EncryptedStr)
    # Two surnames are the Colombian norm; one column, not first/last.
    family_names: Mapped[str] = mapped_column(EncryptedStr)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    phone_e164: Mapped[str | None] = mapped_column(EncryptedStr, nullable=True)
    phone_e164_bidx: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    email: Mapped[str | None] = mapped_column(EncryptedStr, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PhoneBinding(UUIDPrimaryKey, Timestamped, Base):
    """ADR-18: a number is contactability, not identity.

    One handset commonly serves a household, so the mapping is many-to-one.
    """

    __tablename__ = "phone_bindings"
    __table_args__ = (
        Index(
            "uq_phone_bindings_clinic_phone_patient",
            "clinic_id",
            "phone_e164_bidx",
            "patient_id",
            unique=True,
        ),
        {"schema": "app"},
    )

    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.clinics.id"))
    phone_e164_bidx: Mapped[bytes] = mapped_column(LargeBinary)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.patients.id"))
    relationship_kind: Mapped[str] = mapped_column(String(20))  # self | guardian | caregiver
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Consent(UUIDPrimaryKey, Timestamped, Base):
    """ADR-19: consent is an M0/M1 schema entity, not an M5 afterthought.

    Health data is sensitive data under Ley 1581, so authorization must be
    explicit, provable, and tied to the policy version in force when given.
    """

    __tablename__ = "consents"
    __table_args__ = (
        Index(
            "ix_consents_lookup",
            "clinic_id",
            "patient_id",
            "purpose",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        {"schema": "app"},
    )

    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.clinics.id"))
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.patients.id"))
    purpose: Mapped[str] = mapped_column(String(40))
    channel: Mapped[str | None] = mapped_column(String(20), nullable=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_kind: Mapped[str] = mapped_column(String(40))
    evidence_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(40))


class Doctor(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "doctors"

    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.clinics.id"))
    full_name: Mapped[str] = mapped_column(String(200))
    specialty: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Location(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "locations"

    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.clinics.id"))
    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str] = mapped_column(String(300))


class AvailabilityRule(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "availability_rules"
    __table_args__ = (
        CheckConstraint("end_time > start_time", name="time_window_ordered"),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_range"),
        {"schema": "app"},
    )

    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.clinics.id"))
    doctor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.doctors.id"))
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.locations.id"))
    weekday: Mapped[int] = mapped_column(SmallInteger)  # 0 = Monday
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)


class AvailabilityException(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "availability_exceptions"

    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.clinics.id"))
    doctor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.doctors.id"))
    on_date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(20))  # unavailable | extra_hours
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[time | None] = mapped_column(Time, nullable=True)


class AppointmentType(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "appointment_types"

    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.clinics.id"))
    name: Mapped[str] = mapped_column(String(100))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    buffer_minutes: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # 'high' requires a second identity factor before disclosing detail (ADR-18).
    sensitivity: Mapped[str] = mapped_column(String(10), server_default=text("'normal'"))


class Appointment(UUIDPrimaryKey, Timestamped, Base):
    """Bookings and holds share this table so they share one overlap constraint."""

    __tablename__ = "appointments"
    __table_args__ = (
        ExcludeConstraint(
            ("doctor_id", "="),
            ("during", "&&"),
            name="no_overlapping_active_appointments",
            using="gist",
            where=text(f"status IN {ACTIVE_STATUSES}"),
        ),
        CheckConstraint(
            "(status <> 'hold') OR (expires_at IS NOT NULL)",
            name="hold_requires_expiry",
        ),
        {"schema": "app"},
    )

    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.clinics.id"))
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.patients.id"))
    doctor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.doctors.id"))
    location_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.locations.id"))
    appointment_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.appointment_types.id"))
    during: Mapped[object] = mapped_column(TSTZRANGE)
    status: Mapped[str] = mapped_column(String(20))
    # Set only for status='hold'; swept by a background job (M2).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Makes a redelivered webhook idempotent rather than a second booking.
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)

    doctor: Mapped[Doctor] = relationship(lazy="raise")
