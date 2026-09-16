"""Column-level encryption and blind indexing for direct identifiers.

Why this exists in M0 rather than M5: the *shape* of the schema has to be right
from the start. Encryption itself is an M5 deliverable, but retrofitting BYTEA
columns and a lookup index onto a live patients table is a painful migration.
So the columns and the code path exist now, and M5 replaces the key source with
the secrets manager / KMS without touching the schema.

AES-256-GCM with a random 12-byte nonce per value, stored as nonce || ciphertext.
Deterministic encryption is deliberately NOT used; lookup goes through the blind
index instead.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import unicodedata

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import LargeBinary, TypeDecorator
from sqlalchemy.engine import Dialect

from src.core.config import get_settings

_NONCE_BYTES = 12


def _derive_key(raw: str) -> bytes:
    """Derive a 32-byte key from the configured secret.

    ponytail: plain SHA-256 of the configured secret. Adequate because the input
    is already a high-entropy random secret from the secrets manager, not a
    password. Swap for HKDF with a per-tenant salt when per-clinic keys land (M5).
    """
    return hashlib.sha256(raw.encode("utf-8")).digest()


def encrypt(plaintext: str) -> bytes:
    key = _derive_key(get_settings().phi_encryption_key.get_secret_value())
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)


def decrypt(blob: bytes) -> str:
    key = _derive_key(get_settings().phi_encryption_key.get_secret_value())
    nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")


def blind_index(value: str) -> bytes:
    """Deterministic, keyed lookup token for an encrypted column.

    Normalization matters: an inbound webhook looks a patient up by phone number,
    and that number must hash identically however it was stored.
    """
    key = _derive_key(get_settings().phi_blind_index_key.get_secret_value())
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return hmac.new(key, normalized.encode("utf-8"), hashlib.sha256).digest()


class EncryptedStr(TypeDecorator[str]):
    """A string column stored encrypted at rest."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> bytes | None:
        return None if value is None else encrypt(value)

    def process_result_value(self, value: bytes | None, dialect: Dialect) -> str | None:
        return None if value is None else decrypt(value)
