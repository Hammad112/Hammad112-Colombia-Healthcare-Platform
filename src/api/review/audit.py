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
    """Recompute the keyed hash chain and compare it with the newest anchor.

    `first_broken_id` names the first entry that was altered, or before which an
    entry was removed or inserted. `truncated_after_id` names the entry the
    newest anchor witnessed when the log no longer matches it, which is how
    removal of the newest entries is caught. `intact` is true only when neither
    is set, and entries written since the last anchor are not yet covered; see
    src/audit/service.py.
    """
    verification = await audit.verify_chain(session)
    await audit.record_access(
        session, AccessAction.READ, audit.Access(resource="audit_log", resource_id="verify")
    )
    return ChainVerificationOut.build(verification)
