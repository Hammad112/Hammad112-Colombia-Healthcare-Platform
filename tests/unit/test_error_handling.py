"""Failure paths of the application factory. No database is needed."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core.config import Settings
from src.core.db import get_session


class _SessionWhoseCommitFails:
    async def execute(self, *_args: object, **_kwargs: object) -> None:
        return None


async def _session_failing_on_commit() -> AsyncIterator[_SessionWhoseCommitFails]:
    yield _SessionWhoseCommitFails()
    raise RuntimeError("commit failed")


def _client_with_failing_commit() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = _session_failing_on_commit
    return TestClient(app, raise_server_exceptions=False)


def test_failed_commit_turns_the_response_into_500() -> None:
    """The session closes before the response is sent, so a commit failure (which
    would lose audit entries) cannot be preceded by a 200 carrying the data."""
    with _client_with_failing_commit() as client:
        response = client.get("/readyz")
    assert response.status_code == 500


def test_500_response_keeps_request_id_and_security_headers() -> None:
    with _client_with_failing_commit() as client:
        response = client.get("/readyz", headers={"X-Request-ID": "trace-500"})
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error", "request_id": "trace-500"}
    assert response.headers["X-Request-ID"] == "trace-500"
    assert response.headers["Cache-Control"] == "no-store"


def test_openapi_document_is_not_served_outside_synthetic_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_data = Settings(_env_file=None, app_env="local", allow_real_patient_data=True)
    monkeypatch.setattr("src.api.app.get_settings", lambda: real_data)
    with TestClient(create_app()) as client:
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404
