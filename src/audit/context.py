"""Who is accessing patient data, and why (ADR-09, ADR-24).

An `AuditContext` is stored in a context variable for the duration of a request
or job. Repository functions read it when they record an access, so the actor,
purpose and request id are captured without being passed through every call.

Accesses are recorded explicitly by repositories rather than through ORM
events. An event hook sees the statement but not why the data was read, and
cannot tell which rows the caller actually returns to a user.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import StrEnum


class ActorKind(StrEnum):
    STAFF = "staff"
    SYSTEM = "system"
    AGENT = "agent"
    PATIENT = "patient"


class AccessAction(StrEnum):
    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    DISCLOSE = "disclose"  # sent to a third-party processor, such as a model provider


@dataclass(frozen=True, slots=True)
class AuditContext:
    actor_kind: ActorKind
    purpose: str
    actor_id: str | None = None
    clinic_id: uuid.UUID | None = None
    channel: str | None = None
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ip: str | None = None


_current: ContextVar[AuditContext | None] = ContextVar("audit_context", default=None)


def set_context(context: AuditContext) -> Token[AuditContext | None]:
    return _current.set(context)


def reset_context(token: Token[AuditContext | None]) -> None:
    _current.reset(token)


@contextmanager
def audit_context(context: AuditContext) -> Iterator[AuditContext]:
    """Bind `context` for the duration of a block, restoring the previous one after."""
    token = set_context(context)
    try:
        yield context
    finally:
        reset_context(token)


def get_context() -> AuditContext:
    """Return the current context, or raise if none is bound.

    Raising is deliberate: recording an access without knowing who made it
    would produce an audit trail that cannot answer the question it exists for.
    """
    context = _current.get()
    if context is None:
        raise RuntimeError("No AuditContext is bound; patient-data access requires one.")
    return context
