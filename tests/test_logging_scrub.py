"""Patient data must never reach the logs."""

from __future__ import annotations

from src.core.logging import scrub_pii


def test_direct_identifiers_are_redacted() -> None:
    event = {
        "event": "inbound",
        "phone_e164": "+573001112233",
        "given_names": "Ana",
        "transcript": "sí, confirmo",
        "patient_id": "a-uuid",
    }
    scrubbed = scrub_pii(None, "info", dict(event))
    assert scrubbed["phone_e164"] == "[redacted]"
    assert scrubbed["given_names"] == "[redacted]"
    assert scrubbed["transcript"] == "[redacted]"
    # Identifiers that are not personal data survive, so debugging stays possible.
    assert scrubbed["patient_id"] == "a-uuid"
    assert scrubbed["event"] == "inbound"


def test_redaction_is_case_insensitive() -> None:
    scrubbed = scrub_pii(None, "info", {"Phone": "+57300", "EMAIL": "a@b.co"})
    assert scrubbed["Phone"] == "[redacted]"
    assert scrubbed["EMAIL"] == "[redacted]"
