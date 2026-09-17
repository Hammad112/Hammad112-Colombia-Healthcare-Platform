"""Review routes for the audit log. Reading the log is itself recorded."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from src.api.dependencies import PageDep, SessionDep
from src.api.review.schemas import AuditEntryOut, ChainVerificationOut, Page, to_page
from src.audit import service as audit
from src.audit.context import AccessAction

router = APIRouter()


@router.get("/audit-log", response_model=Page[AuditEntryOut])
async def list_audit_log(
    session: SessionDep,
    page: PageDep,
    patient_id: uuid.UUID | None = None,
    action: AccessAction | None = None,
    resource: str | None = None,
) -> Page[AuditEntryOut]:
    """Entries newest first, as they stood before this request's own entry was added."""
    result = await audit.list_entries(
        session,
        page=page,
        patient_id=patient_id,
        action=action.value if action else None,
        resource=resource,
    )
    return to_page(result, [AuditEntryOut.build(entry) for entry in result.items])


@router.get("/audit-log/verify", response_model=ChainVerificationOut)
async def verify_audit_log(session: SessionDep) -> ChainVerificationOut:
    """Recompute the hash chain.

    `intact: false` means an entry was altered, or an entry before the newest was
    removed or inserted. `intact: true` does not rule out removal of the newest
    entries or a fully recomputed chain; see src/audit/service.py.
    """
    verification = await audit.verify_chain(session)
    await audit.record_access(
        session, AccessAction.READ, audit.Access(resource="audit_log", resource_id="verify")
    )
    return ChainVerificationOut.build(verification)
