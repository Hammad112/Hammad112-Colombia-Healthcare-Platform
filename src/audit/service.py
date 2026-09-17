"""Recording and verifying access-log entries.

Tamper evidence
    Each entry stores `row_hash = SHA-256(prev_hash || canonical_json(fields))`,
    where `prev_hash` is the previous entry's `row_hash`. The hash covers every
    stored column except `id`, `prev_hash` and `row_hash` itself.

    `verify_chain` detects an entry that was altered, and an entry deleted or
    inserted anywhere before the newest entry. It does not detect removal of the
    newest entries, and it does not detect a chain rewritten end to end, because
    the hash uses no secret key and the chain tip is not anchored anywhere
    outside the database.

Concurrency
    Extending the chain requires reading its tip, so writers take a
    transaction-scoped PostgreSQL advisory lock first. The lock is released when
    the writing transaction commits or rolls back. This serializes audited
    transactions with each other; unaudited work is unaffected.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.context import AccessAction, get_context
from src.audit.models import AccessLogEntry
from src.core.pagination import PageRequest, PageResult, fetch_page

# Arbitrary application-wide key for pg_advisory_xact_lock.
_CHAIN_LOCK_KEY = 0x0A0D17C4A1


@dataclass(frozen=True, slots=True)
class Access:
    """One record accessed. `patient_id` is set whenever the record is patient data."""

    resource: str
    resource_id: str | None = None
    patient_id: uuid.UUID | None = None
    processor: str | None = None
    processor_model: str | None = None
    fields_disclosed: tuple[str, ...] | None = None
    zero_retention: bool | None = None


@dataclass(frozen=True, slots=True)
class ChainVerification:
    rows_checked: int
    first_broken_id: int | None

    @property
    def intact(self) -> bool:
        return self.first_broken_id is None


def _normalize_ip(value: object) -> str | None:
    """Return a valid IP address string, or None.

    The column type is `inet`. Client addresses are not always IPs (test
    clients, Unix sockets, some proxy setups), and recording one must not make
    the request fail.
    """
    if value is None:
        return None
    try:
        return str(ipaddress.ip_address(str(value)))
    except ValueError:
        return None


def _hashed_fields(entry: AccessLogEntry) -> dict[str, Any]:
    return {
        "occurred_at": entry.occurred_at.astimezone(UTC).isoformat(),
        "clinic_id": str(entry.clinic_id) if entry.clinic_id else None,
        "actor_kind": entry.actor_kind,
        "actor_id": entry.actor_id,
        "patient_id": str(entry.patient_id) if entry.patient_id else None,
        "action": entry.action,
        "resource": entry.resource,
        "resource_id": entry.resource_id,
        "purpose": entry.purpose,
        "channel": entry.channel,
        "request_id": entry.request_id,
        "ip": _normalize_ip(entry.ip),
        "processor": entry.processor,
        "processor_model": entry.processor_model,
        "fields_disclosed": list(entry.fields_disclosed) if entry.fields_disclosed else None,
        "zero_retention": entry.zero_retention,
    }


def _compute_hash(entry: AccessLogEntry, prev_hash: bytes | None) -> bytes:
    canonical = json.dumps(
        _hashed_fields(entry), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256((prev_hash or b"") + canonical.encode("utf-8")).digest()


async def record_accesses(
    session: AsyncSession, action: AccessAction, accesses: Sequence[Access]
) -> None:
    """Append one entry per access, attributed to the bound `AuditContext`.

    Entries are flushed into the caller's transaction and become durable only
    when that transaction commits, together with the work being audited.
    """
    if not accesses:
        return
    context = get_context()

    await session.execute(select(func.pg_advisory_xact_lock(_CHAIN_LOCK_KEY)))
    prev_hash = await session.scalar(
        select(AccessLogEntry.row_hash).order_by(AccessLogEntry.id.desc()).limit(1)
    )
    occurred_at = datetime.now(UTC)

    for access in accesses:
        entry = AccessLogEntry(
            occurred_at=occurred_at,
            clinic_id=context.clinic_id,
            actor_kind=context.actor_kind.value,
            actor_id=context.actor_id,
            patient_id=access.patient_id,
            action=action.value,
            resource=access.resource,
            resource_id=access.resource_id,
            purpose=context.purpose,
            channel=context.channel,
            request_id=context.request_id,
            ip=_normalize_ip(context.ip),
            processor=access.processor,
            processor_model=access.processor_model,
            fields_disclosed=list(access.fields_disclosed) if access.fields_disclosed else None,
            zero_retention=access.zero_retention,
            prev_hash=prev_hash,
        )
        entry.row_hash = _compute_hash(entry, prev_hash)
        session.add(entry)
        # Flush each entry on its own so ids are assigned in chain order;
        # verification walks the chain in id order.
        await session.flush()
        prev_hash = entry.row_hash


async def record_access(session: AsyncSession, action: AccessAction, access: Access) -> None:
    await record_accesses(session, action, [access])


async def verify_chain(session: AsyncSession, batch_size: int = 1000) -> ChainVerification:
    """Recompute every hash in id order, streaming rows in batches."""
    checked = 0
    prev_hash: bytes | None = None
    result = await session.stream_scalars(
        select(AccessLogEntry).order_by(AccessLogEntry.id).execution_options(yield_per=batch_size)
    )
    async for entry in result:
        checked += 1
        if entry.prev_hash != prev_hash or entry.row_hash != _compute_hash(entry, prev_hash):
            await result.close()
            return ChainVerification(rows_checked=checked, first_broken_id=entry.id)
        prev_hash = entry.row_hash
    return ChainVerification(rows_checked=checked, first_broken_id=None)


async def list_entries(
    session: AsyncSession,
    *,
    page: PageRequest,
    patient_id: uuid.UUID | None = None,
    action: str | None = None,
    resource: str | None = None,
) -> PageResult[AccessLogEntry]:
    """Newest entries first. The read itself is recorded as an access."""
    statement = select(AccessLogEntry).order_by(AccessLogEntry.id.desc())
    if patient_id is not None:
        statement = statement.where(AccessLogEntry.patient_id == patient_id)
    if action is not None:
        statement = statement.where(AccessLogEntry.action == action)
    if resource is not None:
        statement = statement.where(AccessLogEntry.resource == resource)
    rows, total = await fetch_page(session, statement, page)
    entries = [row[0] for row in rows]
    await record_access(
        session, AccessAction.READ, Access(resource="audit_log", patient_id=patient_id)
    )
    return PageResult(items=entries, total=total, limit=page.limit, offset=page.offset)
