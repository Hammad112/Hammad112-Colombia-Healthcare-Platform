"""Application settings.

Values are read from the environment. In production the environment is populated
by the secrets manager at runtime (ADR-10); no credential is ever read from a file
committed to the repository.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: Literal["local", "ci", "staging", "production"] = "local"
    log_level: str = "INFO"

    postgres_user: str = "clinic"
    postgres_password: SecretStr = SecretStr("")
    postgres_db: str = "clinic"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    app_db_user: str = "clinic_app"
    app_db_password: SecretStr = SecretStr("")

    phi_encryption_key: SecretStr = SecretStr("")
    phi_blind_index_key: SecretStr = SecretStr("")

    # ADR-11: the hard gate. Real patient data may not be processed until the
    # compliance package is signed (see ARCHITECTURE.md ADR-15).
    allow_real_patient_data: bool = False

    rate_limit_per_minute: int = Field(default=60, ge=1)

    @property
    def database_url(self) -> str:
        pwd = self.postgres_password.get_secret_value()
        return (
            f"postgresql+psycopg://{self.postgres_user}:{pwd}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @model_validator(mode="after")
    def _reject_dev_keys_outside_local(self) -> Settings:
        """Fail fast rather than silently running production on a development key."""
        if self.app_env in ("staging", "production"):
            for name in ("phi_encryption_key", "phi_blind_index_key"):
                value: SecretStr = getattr(self, name)
                raw = value.get_secret_value()
                if not raw or raw.startswith("local-dev-"):
                    raise ValueError(
                        f"{name} is unset or still a local development key while "
                        f"APP_ENV={self.app_env}. Resolve it from the secrets manager."
                    )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
