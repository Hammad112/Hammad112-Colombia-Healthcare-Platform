"""The review API must not exist outside synthetic-data mode. No database is needed:
the access dependency rejects the request before any query runs."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.server import UnsafeBindError, check_bind_address
from src.bootstrap import ProvisioningError, reset_database
from src.core.config import Settings, get_settings

STRONG = "s" * 40


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_env": "staging"},
        {"app_env": "production"},
        {"app_env": "local", "allow_real_patient_data": True},
    ],
)
def test_review_routes_respond_404_outside_synthetic_mode(overrides: dict[str, object]) -> None:
    settings = Settings(
        _env_file=None,
        phi_encryption_key=STRONG,
        phi_blind_index_key=STRONG,
        audit_chain_key=STRONG,
        postgres_password="owner-secret",
        app_db_password="runtime-secret",
        **overrides,  # type: ignore[arg-type]
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        for path in ("/review/summary", "/review/patients", f"/review/patients/{uuid.uuid4()}"):
            assert client.get(path).status_code == 404, path


def test_reset_database_is_refused_outside_synthetic_mode() -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        phi_encryption_key=STRONG,
        phi_blind_index_key=STRONG,
        audit_chain_key=STRONG,
        postgres_password="owner-secret",
        app_db_password="runtime-secret",
    )
    with pytest.raises(ProvisioningError, match="Refusing to reset"):
        reset_database(settings)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_binds_are_allowed(host: str) -> None:
    check_bind_address(host)


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "192.168.1.10", "clinic.example.com"],  # noqa: S104 - the point of the test
)
def test_non_loopback_binds_are_refused_while_the_review_api_is_enabled(host: str) -> None:
    with pytest.raises(UnsafeBindError, match="no authentication"):
        check_bind_address(host)


def test_non_loopback_binds_are_allowed_once_the_review_api_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_REAL_PATIENT_DATA", "true")
    get_settings.cache_clear()
    try:
        check_bind_address("0.0.0.0")  # noqa: S104 - the point of the test
    finally:
        get_settings.cache_clear()
