"""The liveness endpoint responds without touching the database."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import __version__
from app.main import app


def test_health_ok() -> None:
    # No context manager: the lifespan (and dev schema creation) does not run.
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
