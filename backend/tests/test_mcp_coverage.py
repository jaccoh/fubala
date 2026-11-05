"""Every /api/v1 operation has an MCP tool or a written exclusion."""

from fastapi import FastAPI

from app import mcp_tools
from app.main import create_app
from app.mcp_tools import EXCLUDED, ROUTE_TOOLS, TOOL_ONLY, uncovered_routes


def test_every_route_has_a_tool_or_an_exclusion(tmp_path, monkeypatch):
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "cov.db"))
    assert uncovered_routes(create_app()) == []


def test_excluded_holds_only_probes_browser_login_and_credentials():
    assert set(EXCLUDED) == {
        "GET /api/v1/health",
        "GET /api/v1/health/live",
        "GET /api/v1/version",
        "GET /api/v1/ping",
        "POST /api/v1/auth/session",
        "POST /api/v1/auth/token",
        "DELETE /api/v1/auth/token",
    }
    assert all(reason.strip() for reason in EXCLUDED.values())
    assert not set(EXCLUDED) & set(ROUTE_TOOLS)


def test_every_mapped_tool_exists():
    names = {fn.__name__ for fn in mcp_tools.TOOLS}
    assert set(ROUTE_TOOLS.values()) <= names


def test_unmapped_route_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "cov2.db"))
    app: FastAPI = create_app()

    @app.get("/api/v1/zz-probe")
    def zz_probe() -> dict:
        return {}

    assert uncovered_routes(app) == ["GET /api/v1/zz-probe"]


def test_stale_mapping_is_reported():
    app = FastAPI()
    stale = uncovered_routes(app)
    assert stale and all(entry.startswith("stale: ") for entry in stale)


def test_tool_only_entries_are_tools_without_a_route():
    names = {fn.__name__ for fn in mcp_tools.TOOLS}
    assert "get_fubala_instructions" in TOOL_ONLY
    for name, reason in TOOL_ONLY.items():
        assert name in names
        assert name not in ROUTE_TOOLS.values()
        assert reason.strip()
