"""M0 exit criterion: the service starts and answers health checks."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz_is_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_request_id_header_is_returned(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert response.headers["X-Request-ID"] == "abc-123"


def test_request_id_is_generated_when_absent(client: TestClient) -> None:
    response = client.get("/healthz")
    assert len(response.headers["X-Request-ID"]) >= 16


def test_oversized_body_is_rejected_before_parsing(client: TestClient) -> None:
    response = client.post(
        "/healthz",
        content=b"x",
        headers={"Content-Length": str(10_000_000)},
    )
    assert response.status_code == 413
