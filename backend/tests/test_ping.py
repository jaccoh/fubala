"""Liveness probe: assert the API is up and mounted under /api/v1."""

from fastapi.testclient import TestClient


def test_ping_returns_pong(client: TestClient):
    res = client.get("/api/v1/ping")
    assert res.status_code == 200
    assert res.json() == {"ping": "pong"}
