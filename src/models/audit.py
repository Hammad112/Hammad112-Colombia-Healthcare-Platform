"""Append-only audit log (ADR-09, ADR-24).

Lives in its own `audit` schema. The application role is granted INSERT only,
and a trigger raises on UPDATE or DELETE, so the log is append-only by two
independent mechanisms rather than by convention.

`processor` / `fields_disclosed` record disclosures to third-party model and
speech providers. Prompt bodies are deliberately NOT stored: the point is to
answer "who saw what", not to create a second copy of the patient data.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, LargeBinary, MetaData, String, func
from sqlalchemy.dialects.postgresql import ARRAY, INET
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.models.base import NAMING_CONVENTION


class AuditBase(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION, schema="audit")


class AccessLog(AuditBase):
    __tablename__ = "access_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    clinic_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    actor_kind: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    patient_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(String(20))
    resource: Mapped[str] = mapped_column(String(60))
    resource_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    purpose: Mapped[str] = mapped_column(String(60))
    channel: Mapped[str | None] = mapped_column(String(20), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)

    # Third-party disclosure inventory (ADR-24)
    processor: Mapped[str | None] = mapped_column(String(60), nullable=True)
    processor_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    fields_disclosed: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    zero_retention: Mapped[bool | None] = mapped_column(nullable=True)

    # Tamper evidence: each row chains to the previous one.
    prev_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    row_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
