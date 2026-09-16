"""Encryption and blind-index behaviour.

The blind index is what makes an encrypted phone column still usable for the
inbound-webhook lookup, so its normalization behaviour is a correctness
requirement, not a detail.
"""

from __future__ import annotations

from src.core.crypto import blind_index, decrypt, encrypt


def test_roundtrip_preserves_value() -> None:
    assert decrypt(encrypt("Ana María Ñuñez")) == "Ana María Ñuñez"


def test_encryption_is_non_deterministic() -> None:
    # Equal plaintexts must not produce equal ciphertexts, or the column leaks
    # equality and becomes a de-facto index.
    assert encrypt("+573001112233") != encrypt("+573001112233")


def test_blind_index_is_deterministic() -> None:
    assert blind_index("+573001112233") == blind_index("+573001112233")


def test_blind_index_normalizes_case_and_whitespace() -> None:
    assert blind_index(" AB@example.com ") == blind_index("ab@example.com")


def test_blind_index_separates_distinct_values() -> None:
    assert blind_index("+573001112233") != blind_index("+573001112234")
