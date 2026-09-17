from __future__ import annotations

from src.api.review.masking import mask_email, mask_tail
from src.core.logging import redact_identifiers
from src.core.text import contains_pattern


def test_contains_pattern_escapes_like_wildcards() -> None:
    assert contains_pattern("Pedia") == "%Pedia%"
    assert contains_pattern("100%_x") == "%100\\%\\_x%"
    assert contains_pattern("a\\b") == "%a\\\\b%"


def test_mask_tail_keeps_last_four() -> None:
    assert mask_tail("1020304050") == "******4050"
    assert mask_tail("+573001112233") == "*********2233"


def test_mask_tail_hides_short_values_entirely() -> None:
    assert mask_tail("1234") == "****"
    assert mask_tail("") == ""
    assert mask_tail(None) is None


def test_mask_email() -> None:
    assert mask_email("ana.gomez@example.com") == "a***@example.com"
    assert mask_email("not-an-email") == "********mail"
    assert mask_email(None) is None


def test_log_redaction_by_key() -> None:
    event = {
        "event": "inbound",
        "phone_e164": "+573001112233",
        "Given_Names": "Ana",
        "patient_id": "p1",
    }
    redacted = redact_identifiers(None, "info", event)
    assert redacted["phone_e164"] == "[redacted]"
    assert redacted["Given_Names"] == "[redacted]"
    assert redacted["patient_id"] == "p1"
