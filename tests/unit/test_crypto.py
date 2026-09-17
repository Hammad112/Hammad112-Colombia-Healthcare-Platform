from __future__ import annotations

import pytest

from src.core.crypto import blind_index, decrypt, encrypt


def test_round_trip_preserves_spanish_text() -> None:
    assert decrypt(encrypt("María José Ñúñez")) == "María José Ñúñez"


def test_encryption_is_randomized() -> None:
    # Equal ciphertexts would reveal which patients share a value.
    assert encrypt("+573001112233") != encrypt("+573001112233")


def test_ciphertext_starts_with_key_version() -> None:
    assert encrypt("x")[0] == 1


def test_unknown_key_version_is_rejected() -> None:
    blob = bytearray(encrypt("x"))
    blob[0] = 99
    with pytest.raises(ValueError, match="version"):
        decrypt(bytes(blob))


def test_tampered_ciphertext_is_rejected() -> None:
    from cryptography.exceptions import InvalidTag

    blob = bytearray(encrypt("1020304050"))
    blob[-1] ^= 0x01
    with pytest.raises(InvalidTag):
        decrypt(bytes(blob))


def test_blind_index_is_deterministic_and_distinguishes_values() -> None:
    assert blind_index("+573001112233") == blind_index("+573001112233")
    assert blind_index("+573001112233") != blind_index("+573001112234")


def test_blind_index_ignores_case_and_surrounding_whitespace() -> None:
    assert blind_index("  Ana@Example.com ") == blind_index("ana@example.com")


def test_blind_index_does_not_normalize_phone_formatting() -> None:
    # Callers must canonicalize to E.164 first; this documents that contract.
    assert blind_index("+57 300 111 2233") != blind_index("+573001112233")
