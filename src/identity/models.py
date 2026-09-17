"""Who may be contacted on which number, and on what legal basis.

A phone number establishes contactability, not identity (ADR-18): one handset
is often shared by a household, so a number may be bound to several patients.

Consent is recorded per purpose (ADR-19). Health data is sensitive data under
Ley 1581 de 2012, so each record keeps how consent was obtained and which
version of the data-processing policy was in force.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, LargeBinary, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from src.core.db import Base, string_enum


class BindingRelationship(StrEnum):
    SELF = "self"
    GUARDIAN = "guardian"
    CAREGIVER = "caregiver"


class ConsentPurpose(StrEnum):
    APPOINTMENT_MESSAGING = "appointment_messaging"
    WELLNESS_CHECKINS = "wellness_checkins"
    SENSITIVE_DATA = "sensitive_data"
    TELEMEDICINE = "telemedicine"


class ConsentChannel(StrEnum):
    WHATSAPP = "whatsapp"
    SMS = "sms"
    EMAIL = "email"
    ANY = "any"


class EvidenceKind(StrEnum):
    WRITTEN = "written"
    VERBAL_RECORDED = "verbal_recorded"
    DIGITAL_FORM = "digital_form"
    IMPORTED_DECLARATION = "imported_declaration"


class PhoneBinding(Base):
    __tablename__ = "phone_bindings"
    __table_args__ = (
        Index(
            "uq_phone_bindings_clinic_id_phone_patient",
            "clinic_id",
            "phone_e164_bidx",
            "patient_id",
            unique=True,
        ),
        Index("ix_phone_bindings_patient_id", "patient_id"),
        {"schema": "app"},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.clinics.id"))
    # Blind index of the E.164 number. The number itself is not stored here; for
    # a `self` binding it matches the patient's own encrypted `phone_e164`, while
    # guardian or caregiver bindings may index a number stored on no patient.
    phone_e164_bidx: Mapped[bytes] = mapped_column(LargeBinary)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.patients.id"))
    relationship_kind: Mapped[BindingRelationship] = mapped_column(
        string_enum(BindingRelationship, "relationship_kind_valid")
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Consent(Base):
    __tablename__ = "consents"
    __table_args__ = (
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= granted_at", name="revoked_after_granted"
        ),
        Index("ix_consents_patient_id_purpose", "patient_id", "purpose"),
        {"schema": "app"},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.clinics.id"))
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.patients.id"))
    purpose: Mapped[ConsentPurpose] = mapped_column(string_enum(ConsentPurpose, "purpose_valid"))
    channel: Mapped[ConsentChannel] = mapped_column(string_enum(ConsentChannel, "channel_valid"))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_kind: Mapped[EvidenceKind] = mapped_column(
        string_enum(EvidenceKind, "evidence_kind_valid")
    )
    # Reference to the stored evidence, such as an object-storage key.
    evidence_ref: Mapped[str | None] = mapped_column(String(500))
    policy_version: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
