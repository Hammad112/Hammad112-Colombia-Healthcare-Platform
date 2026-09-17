"""Structured JSON logging with redaction of known identifier fields.

Redaction is by key name: a value logged under one of `_REDACTED_KEYS` is
replaced with "[redacted]". It does not inspect free text, so patient data
must never be interpolated into the event message itself; log identifiers
such as `patient_id` instead.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any

import structlog

_REDACTED_KEYS = frozenset(
    {
        "phone",
        "phone_e164",
        "email",
        "document_number",
        "given_names",
        "family_names",
        "full_name",
        "message_body",
        "transcript",
        "prompt",
        "password",
    }
)


def redact_identifiers(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key.lower() in _REDACTED_KEYS:
            event_dict[key] = "[redacted]"
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    numeric_level = logging.getLevelName(level.upper())
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO
    logging.basicConfig(format="%(message)s", level=numeric_level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_identifiers,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger
