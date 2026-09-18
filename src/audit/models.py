"""Append-only access log (ADR-09, ADR-24).

The tables live in the `audit` schema. For the runtime role both are
append-only: the migrations grant that role only SELECT and INSERT, and a
trigger rejects UPDATE and DELETE from any role. The owner account can still
TRUNCATE a table or disable a trigger, which is why `chain_anchor` exists: the
keyed chain in src/audit/service.py detects altered entries and gaps, and the
anchors detect the truncation the chain alone cannot see.

`processor`, `processor_model`, `fields_disclosed` and `zero_retention` describe
disclosures to third-party processors such as model or speech providers. Prompt
and message bodies are deliberately not stored here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    Identity,
    Index,
    LargeBinary,
    String,
    Uuid,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from src.core.db import Base


class AccessLogEntry(Base):
    __tablename__ = "access_log"
    __table_args__ = (
        Index("ix_access_log_patient_id_occurred_at", "patient_id", "occurred_at"),
        {"schema": "audit"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Set by the application, not the database, so the value is covered by row_hash.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    clinic_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    actor_kind: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(120))
    patient_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(20))
    resource: Mapped[str] = mapped_column(String(60))
    resource_id: Mapped[str | None] = mapped_column(String(120))
    purpose: Mapped[str] = mapped_column(String(60))
    channel: Mapped[str | None] = mapped_column(String(30))
    request_id: Mapped[str | None] = mapped_column(String(128))
    ip: Mapped[str | None] = mapped_column(INET)

    processor: Mapped[str | None] = mapped_column(String(60))
    processor_model: Mapped[str | None] = mapped_column(String(80))
    fields_disclosed: Mapped[list[str] | None] = mapped_column(ARRAY(String(60)))
    zero_retention: Mapped[bool | None] = mapped_column(Boolean)

    prev_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    row_hash: Mapped[bytes] = mapped_column(LargeBinary)


class ChainAnchor(Base):
    """A witness to the state of the access log at one point in time.

    An anchor records the id and hash of the newest access-log entry when it was
    taken. The chain alone cannot detect removal of its newest entries, because
    what remains is still internally consistent. An anchor makes that removal
    visible: the entry it names is either missing or no longer carries the hash
    the anchor recorded.

    Anchors are also written to the application log, so a shipped copy survives
    outside the database. An attacker who can truncate both tables still cannot
    reach that copy.
    """

    __tablename__ = "chain_anchor"
    __table_args__ = ({"schema": "audit"},)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    anchored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Null when the log was empty: an anchor over nothing still proves the log
    # held nothing at that moment.
    entry_id: Mapped[int | None] = mapped_column(BigInteger)
    row_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    entry_count: Mapped[int] = mapped_column(BigInteger)
