"""python -m app.mcp is a thin stdio-to-HTTP proxy with no tools of its own."""

import inspect

import anyio
from mcp.client import Client

from app import mcp as proxy
from app.main import create_app
from app.mcp_tools import ROUTE_TOOLS, build_mcp_server, load_instructions


def test_proxy_forwards_tools_list(tmp_path, monkeypatch):
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "proxy.db"))
    upstream_server = build_mcp_server(create_app())

    async def run() -> set[str]:
        async with Client(upstream_server) as upstream:
            async with Client(proxy.make_proxy_server(upstream)) as client:
                return {t.name for t in (await client.list_tools()).tools}

    assert set(ROUTE_TOOLS.values()) <= anyio.run(run)


def test_proxy_holds_no_tool_bodies():
    source = inspect.getsource(proxy)
    assert "add_tool" not in source and ".tool(" not in source
    assert "import httpx" not in source


def test_mcp_url_defaults_from_api_url(monkeypatch):
    monkeypatch.delenv("FUBALA_MCP_URL", raising=False)
    monkeypatch.setenv("FUBALA_API_URL", "https://fubala.example.com/api/v1")
    assert proxy.mcp_url() == "https://fubala.example.com/mcp"
    monkeypatch.setenv("FUBALA_MCP_URL", "http://x/custom")
    assert proxy.mcp_url() == "http://x/custom"


def test_proxy_passes_instructions_through(tmp_path, monkeypatch):
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "proxy2.db"))
    upstream_server = build_mcp_server(create_app())

    async def run() -> tuple[str | None, str | None]:
        async with Client(upstream_server) as upstream:
            fetched = await proxy.fetch_instructions(upstream)
            async with Client(proxy.make_proxy_server(upstream, fetched)) as client:
                return fetched, client.instructions

    fetched, seen = anyio.run(run)
    assert fetched == load_instructions()
    assert seen == fetched


def test_proxy_instructions_default_to_none_and_fetch_failure_is_soft(capsys):
    class Broken:
        async def call_tool(self, name, arguments):
            raise RuntimeError("down")

    assert anyio.run(proxy.fetch_instructions, Broken()) is None
    assert "could not fetch instructions" in capsys.readouterr().err
