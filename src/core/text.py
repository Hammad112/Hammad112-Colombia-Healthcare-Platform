"""Helpers for building SQL text patterns from user input."""

from __future__ import annotations

LIKE_ESCAPE = "\\"


def contains_pattern(value: str) -> str:
    """A LIKE/ILIKE pattern matching `value` anywhere, with wildcards in it escaped.

    Use with `column.ilike(contains_pattern(value), escape=LIKE_ESCAPE)` so that a
    `%` or `_` typed by a user matches literally.
    """
    escaped = (
        value.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"
