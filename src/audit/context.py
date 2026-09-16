"""Request-scoped audit context (ADR-09, ADR-24).

Read auditing happens at the service/repository layer, not through ORM events:
a read that never loads an entity still discloses data, and ORM events cannot
see the purpose. Every request builds an AuditContext; repository functions that
return patient rows emit an audit record explicitly.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

ActorKind = Literal["staff", "system", "agent", "patient"]
Action = Literal["read", "create", "update", "delete", "disclose"]


@dataclass(frozen=True, slots=True)
class AuditContext:
    actor_kind: ActorKind
    actor_id: str | None = None
    clinic_id: uuid.UUID | None = None
    purpose: str = "unspecified"
    channel: str | None = None
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ip: str | None = None


_ctx: ContextVar[AuditContext | None] = ContextVar("audit_context", default=None)


def set_context(ctx: AuditContext) -> None:
    _ctx.set(ctx)


def get_context() -> AuditContext:
    ctx = _ctx.get()
    if ctx is None:
        # Fail loudly: an unaudited patient-data path is a compliance defect,
        # not something to paper over with a default.
        raise RuntimeError(
            "No AuditContext set for this request. Patient-data access requires one."
        )
    return ctx


def try_get_context() -> AuditContext | None:
    return _ctx.get()
