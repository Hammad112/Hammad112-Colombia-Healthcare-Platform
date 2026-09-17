from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.config import Settings

STRONG_KEY = "k" * 40


def _production(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "production",
        "phi_encryption_key": STRONG_KEY,
        "phi_blind_index_key": STRONG_KEY + "x",
        "postgres_password": "owner-secret",
        "app_db_password": "runtime-secret",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_production_accepts_strong_secrets() -> None:
    assert _production().app_env == "production"


@pytest.mark.parametrize(
    "overrides",
    [
        {"phi_encryption_key": "short"},
        {"phi_blind_index_key": "local-dev-bidx-not-for-production-00000000"},
        {"postgres_password": ""},
        {"app_db_password": "change-me-locally-app"},
    ],
)
def test_production_rejects_weak_or_placeholder_secrets(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _production(**overrides)


def test_local_allows_placeholders() -> None:
    settings = Settings(_env_file=None, app_env="local", phi_encryption_key="local-dev-key")
    assert settings.app_env == "local"


@pytest.mark.parametrize(
    ("app_env", "allow_real", "expected"),
    [
        ("local", False, True),
        ("ci", False, True),
        ("local", True, False),
        ("staging", False, False),
    ],
)
def test_synthetic_data_mode(app_env: str, allow_real: bool, expected: bool) -> None:
    settings = Settings(
        _env_file=None,
        app_env=app_env,  # type: ignore[arg-type]
        allow_real_patient_data=allow_real,
        phi_encryption_key=STRONG_KEY,
        phi_blind_index_key=STRONG_KEY,
        postgres_password="owner-secret",
        app_db_password="runtime-secret",
    )
    assert settings.synthetic_data_mode is expected


def test_real_patient_data_gate_defaults_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_REAL_PATIENT_DATA", raising=False)
    assert Settings(_env_file=None).allow_real_patient_data is False


def test_database_urls_escape_special_characters_and_use_separate_accounts() -> None:
    settings = Settings(
        _env_file=None,
        postgres_host="localhost",
        postgres_user="owner",
        postgres_password="p@ss:w/rd",
        app_db_user="runtime",
        app_db_password="x@y",
    )
    admin = settings.admin_database_url
    app = settings.app_database_url
    assert (admin.username, admin.password, admin.host) == ("owner", "p@ss:w/rd", "localhost")
    assert (app.username, app.password) == ("runtime", "x@y")
    assert "p%40ss%3Aw%2Frd" in admin.render_as_string(hide_password=False)


def test_secrets_are_hidden_in_repr() -> None:
    settings = Settings(_env_file=None, postgres_password="visible-if-leaked")
    assert "visible-if-leaked" not in repr(settings)
