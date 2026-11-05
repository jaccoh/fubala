"""GET /api/v1/version: returns the app version (single source of truth)."""

from app import __version__


def test_version_returns_app_version(client):
    r = client.get("/api/v1/version")
    assert r.status_code == 200
    assert r.json() == {"version": __version__}


def test_version_tagged_system_in_openapi(client):
    # The schema itself is served at the default root URL; only the API routes
    # carry the /api/v1 prefix (main.py does not set openapi_url).
    r = client.get("/openapi.json")
    assert r.status_code == 200
    tags = r.json()["paths"]["/api/v1/version"]["get"]["tags"]
    assert tags == ["system"]
