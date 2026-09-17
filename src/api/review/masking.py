"""Masking of direct identifiers in review responses."""

from __future__ import annotations

VISIBLE_TAIL = 4


def mask_tail(value: str | None, visible: int = VISIBLE_TAIL) -> str | None:
    """Replace every character except the last `visible` with `*`.

    Values no longer than `visible` are masked completely, so a short value is
    never shown in full.
    """
    if value is None:
        return None
    if len(value) <= visible:
        return "*" * len(value)
    return "*" * (len(value) - visible) + value[-visible:]


def mask_email(value: str | None) -> str | None:
    """Keep the first character of the local part and the full domain."""
    if value is None:
        return None
    local, separator, domain = value.partition("@")
    if not separator or not local:
        return mask_tail(value)
    return f"{local[0]}***@{domain}"
