"""Column-level encryption and blind indexing for direct patient identifiers.

Ciphertext format, stored in BYTEA columns:

    version (1 byte) || nonce (12 bytes) || AES-256-GCM ciphertext and tag

The version byte identifies the key, so keys can be rotated later by adding a
version without re-reading data to guess which key encrypted it. Only version 1
exists today.

Encryption is randomized: equal plaintexts produce different ciphertexts, so an
encrypted column reveals nothing about equality. Exact-match lookup goes
through a separate blind-index column holding an HMAC-SHA256 of the value.

Keys are derived from the configured secrets with SHA-256 and a per-purpose
label, so the encryption key and the blind-index key are independent even if
the same secret were configured for both. The configured secrets must be
high-entropy random values; this derivation is not a password hash.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import unicodedata
from functools import cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import LargeBinary
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from src.core.config import get_settings

_KEY_VERSION = 1
_NONCE_BYTES = 12


@cache
def _derive_key(secret: str, label: bytes) -> bytes:
    return hashlib.sha256(label + b"\x00" + secret.encode("utf-8")).digest()


def _encryption_key() -> bytes:
    secret = get_settings().phi_encryption_key.get_secret_value()
    return _derive_key(secret, b"phi-encryption-v1")


def _blind_index_key() -> bytes:
    secret = get_settings().phi_blind_index_key.get_secret_value()
    return _derive_key(secret, b"phi-blind-index-v1")


def encrypt_bytes(plaintext: bytes) -> bytes:
    """Encrypt arbitrary bytes in the format described above."""
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(_encryption_key()).encrypt(nonce, plaintext, None)
    return bytes([_KEY_VERSION]) + nonce + ciphertext


def decrypt_bytes(blob: bytes) -> bytes:
    if not blob or blob[0] != _KEY_VERSION:
        raise ValueError("Unsupported ciphertext version")
    nonce = blob[1 : 1 + _NONCE_BYTES]
    ciphertext = blob[1 + _NONCE_BYTES :]
    return AESGCM(_encryption_key()).decrypt(nonce, ciphertext, None)


def encrypt(plaintext: str) -> bytes:
    return encrypt_bytes(plaintext.encode("utf-8"))


def decrypt(blob: bytes) -> str:
    return decrypt_bytes(blob).decode("utf-8")


def normalize_for_index(value: str) -> str:
    """Unicode NFKC, surrounding whitespace removed, case-folded."""
    return unicodedata.normalize("NFKC", value).strip().casefold()


def blind_index(value: str) -> bytes:
    """Keyed, deterministic lookup token for an encrypted value.

    Normalization covers case, Unicode form and surrounding whitespace only.
    Callers must pass values in their canonical form first, for example phone
    numbers in E.164 (`+573001112233`), or equal numbers will not match.
    """
    data = normalize_for_index(value).encode("utf-8")
    return hmac.new(_blind_index_key(), data, hashlib.sha256).digest()


class EncryptedString(TypeDecorator[str]):
    """A text value stored encrypted in a BYTEA column."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> bytes | None:
        return None if value is None else encrypt(value)

    def process_result_value(self, value: bytes | None, dialect: Dialect) -> str | None:
        return None if value is None else decrypt(value)
