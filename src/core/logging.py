"""Structured JSON logging.

Patient data must never reach the logs. `scrub_pii` drops the keys that carry
direct identifiers; anything genuinely needed for debugging is referenced by id.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any

import structlog

_FORBIDDEN_KEYS = frozenset(
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
    }
)


def scrub_pii(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key.lower() in _FORBIDDEN_KEYS:
            event_dict[key] = "[redacted]"
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            scrub_pii,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
