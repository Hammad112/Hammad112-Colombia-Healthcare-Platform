"""ADR-11 and ADR-10 guardrails that must fail loudly rather than degrade quietly."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.config import Settings


def test_production_rejects_local_development_keys() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(
            app_env="production",
            phi_encryption_key="local-dev-key-not-for-production-0000",
            phi_blind_index_key="local-dev-bidx-not-for-production-000",
        )
    assert "secrets manager" in str(exc.value)


def test_production_rejects_empty_keys() -> None:
    with pytest.raises(ValidationError):
        Settings(app_env="production", phi_encryption_key="", phi_blind_index_key="")


def test_local_allows_development_keys() -> None:
    settings = Settings(
        app_env="local",
        phi_encryption_key="local-dev-key-not-for-production-0000",
        phi_blind_index_key="local-dev-bidx-not-for-production-000",
    )
    assert settings.app_env == "local"


def test_real_patient_data_is_off_by_default() -> None:
    # ADR-11: the gate defaults closed. Turning it on must be a deliberate act.
    assert Settings(_env_file=None).allow_real_patient_data is False


def test_database_url_does_not_leak_password_in_repr() -> None:
    settings = Settings(postgres_password="sup3rsecret")
    assert "sup3rsecret" not in repr(settings)
