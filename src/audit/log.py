"""Audit writer and verifier (ADR-09, ADR-24).

Rows are hash-chained: each row's hash covers its own content plus the previous
row's hash, so removing or altering a row in the middle breaks the chain even if
someone bypasses the trigger and the grants. `verify_chain` proves it.
"""

from __future__ import annotations

import hashlib
import ipaddress
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


def _valid_ip(value: str | None) -> str | None:
    """The column is `inet`. Client hosts are not always IPs (test clients, Unix
    sockets, some proxies), and an unparseable value must not make an audited
    read fail, so anything that is not an IP is recorded as unknown."""
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _payload(
    *,
    clinic_id: uuid.UUID | None,
    actor_kind: str,
    actor_id: str | None,
    patient_id: uuid.UUID | None,
    action: str,
    resource: str,
    resource_id: str | None,
    purpose: str,
    channel: str | None,
    request_id: str | None,
    processor: str | None,
    processor_model: str | None,
    fields_disclosed: list[str] | None,
    zero_retention: bool | None,
) -> dict[str, Any]:
    """The exact fields covered by the hash. Shared by emit and verify."""
    return {
        "clinic_id": str(clinic_id) if clinic_id else None,
        "actor_kind": actor_kind,
        "actor_id": actor_id,
        "patient_id": str(patient_id) if patient_id else None,
        "action": action,
        "resource": resource,
        "resource_id": resource_id,
        "purpose": purpose,
        "channel": channel,
        "request_id": request_id,
        "processor": processor,
        "processor_model": processor_model,
        "fields_disclosed": fields_disclosed,
        "zero_retention": zero_retention,
    }


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

    # ponytail: reads the chain tip per row, so concurrent writers can fork the
    # chain. Fine for one API process; add a row lock or advisory lock on the
    # tip when multiple workers write audit rows (before M4).
    prev = (
        await session.execute(select(AccessLog.row_hash).order_by(AccessLog.id.desc()).limit(1))
    ).scalar_one_or_none()

    payload = _payload(
        clinic_id=ctx.clinic_id,
        actor_kind=ctx.actor_kind,
        actor_id=ctx.actor_id,
        patient_id=patient_id,
        action=action,
        resource=resource,
        resource_id=resource_id,
        purpose=ctx.purpose,
        channel=ctx.channel,
        request_id=ctx.request_id,
        processor=processor,
        processor_model=processor_model,
        fields_disclosed=fields_disclosed,
        zero_retention=zero_retention,
    )

    entry = AccessLog(
        **payload,
        ip=_valid_ip(ctx.ip),
        prev_hash=prev,
        row_hash=_row_hash(payload, prev),
    )
    session.add(entry)
    await session.flush()
    return entry


async def verify_chain(session: AsyncSession) -> tuple[int, int | None]:
    """Recompute every row hash in order.

    Returns (rows_checked, first_broken_row_id). A broken id means a row was
    altered, deleted or inserted out of band at or before that point.

    ponytail: loads the whole log; page through it by id when it grows large.
    """
    rows = (await session.scalars(select(AccessLog).order_by(AccessLog.id))).all()
    prev: bytes | None = None
    for row in rows:
        payload = _payload(
            clinic_id=row.clinic_id,
            actor_kind=row.actor_kind,
            actor_id=row.actor_id,
            patient_id=row.patient_id,
            action=row.action,
            resource=row.resource,
            resource_id=row.resource_id,
            purpose=row.purpose,
            channel=row.channel,
            request_id=row.request_id,
            processor=row.processor,
            processor_model=row.processor_model,
            fields_disclosed=row.fields_disclosed,
            zero_retention=row.zero_retention,
        )
        if row.prev_hash != prev or row.row_hash != _row_hash(payload, prev):
            return len(rows), row.id
        prev = row.row_hash
    return len(rows), None


def audit_context_present() -> bool:
    """Used by tests to assert a code path runs inside an audited request."""
    return try_get_context() is not None
