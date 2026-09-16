"""Audit writer (ADR-09, ADR-24).

Rows are hash-chained: each row's hash covers its own content plus the previous
row's hash, so removing or altering a row in the middle breaks the chain even if
someone bypasses the trigger and the grants.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.context import Action, get_context, try_get_context
from src.models.audit import AccessLog


def _row_hash(payload: dict[str, Any], prev: bytes | None) -> bytes:
    h = hashlib.sha256()
    h.update(prev or b"")
    for key in sorted(payload):
        h.update(f"{key}={payload[key]!r};".encode())
    return h.digest()


async def emit(
    session: AsyncSession,
    *,
    action: Action,
    resource: str,
    resource_id: str | None = None,
    patient_id: uuid.UUID | None = None,
    processor: str | None = None,
    processor_model: str | None = None,
    fields_disclosed: list[str] | None = None,
    zero_retention: bool | None = None,
) -> AccessLog:
    """Record one access event. Call this from the repository layer, explicitly."""
    ctx = get_context()

    prev = (
        await session.execute(
            select(AccessLog.row_hash).order_by(AccessLog.id.desc()).limit(1)
        )
    ).scalar_one_or_none()

    payload: dict[str, Any] = {
        "clinic_id": str(ctx.clinic_id) if ctx.clinic_id else None,
        "actor_kind": ctx.actor_kind,
        "actor_id": ctx.actor_id,
        "patient_id": str(patient_id) if patient_id else None,
        "action": action,
        "resource": resource,
        "resource_id": resource_id,
        "purpose": ctx.purpose,
        "channel": ctx.channel,
        "request_id": ctx.request_id,
        "processor": processor,
        "processor_model": processor_model,
        "fields_disclosed": fields_disclosed,
        "zero_retention": zero_retention,
    }

    entry = AccessLog(
        **payload,
        ip=ctx.ip,
        prev_hash=prev,
        row_hash=_row_hash(payload, prev),
    )
    session.add(entry)
    await session.flush()
    return entry


def audit_context_present() -> bool:
    """Used by tests to assert a code path runs inside an audited request."""
    return try_get_context() is not None
