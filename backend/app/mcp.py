"""Deprecated stdio MCP proxy. The Fubala server itself speaks MCP over
streamable HTTP at /mcp (same Bearer auth as /api/v1); prefer:

    claude mcp add --transport http fubala https://fubala.hoeve.nu/mcp \\
        --header "Authorization: Bearer <token>"

This module stays for one release as a thin stdio-to-HTTP bridge for clients
that can only launch a command. It holds no tools: `tools/list` and
`tools/call` are forwarded to the HTTP endpoint.

    uv run python -m app.mcp

Environment:
    FUBALA_API_URL    API base, default http://localhost:8000/api/v1
    FUBALA_MCP_URL    MCP endpoint, default FUBALA_API_URL with /api/v1 -> /mcp
    FUBALA_API_TOKEN  Bearer token (needed when api_auth_enabled and the URL
                      is not loopback)

Everything the bridge says goes to stderr: stdout carries the protocol.
"""

from __future__ import annotations

import os
import sys

import anyio
import mcp_types as types
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared._httpx_utils import create_mcp_http_client

DEPRECATION = (
    "fubala: the stdio MCP server (python -m app.mcp) is deprecated and will be"
    " removed next release; the Fubala server speaks MCP over HTTP at /mcp."
    " Use: claude mcp add --transport http fubala <host>/mcp"
    ' --header "Authorization: Bearer <token>"'
)


def mcp_url() -> str:
    explicit = os.environ.get("FUBALA_MCP_URL", "").strip()
    if explicit:
        return explicit
    api_url = os.environ.get("FUBALA_API_URL", "http://localhost:8000/api/v1").rstrip("/")
    base = api_url[: -len("/api/v1")] if api_url.endswith("/api/v1") else api_url
    return base + "/mcp"


def _headers() -> dict[str, str]:
    token = os.environ.get("FUBALA_API_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def make_proxy_server(upstream: Client, instructions: str | None = None) -> Server:
    """A low-level server whose tools/list and tools/call go to `upstream`."""

    async def on_list_tools(ctx, params: types.PaginatedRequestParams | None):
        return await upstream.list_tools(cursor=params.cursor if params else None)

    async def on_call_tool(ctx, params: types.CallToolRequestParams):
        return await upstream.call_tool(params.name, params.arguments)

    return Server(
        "fubala",
        instructions=instructions,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def fetch_instructions(upstream: Client) -> str | None:
    """The upstream's working rules, or None (with a warning) if unavailable."""
    try:
        result = await upstream.call_tool("get_fubala_instructions", {})
        if result.is_error:
            raise RuntimeError("get_fubala_instructions returned an error")
        return "".join(c.text for c in result.content if isinstance(c, types.TextContent))
    except Exception as e:  # the bridge must still start without instructions
        print(f"fubala: could not fetch instructions: {e}", file=sys.stderr)
        return None


async def _serve() -> None:
    transport = streamable_http_client(
        mcp_url(), http_client=create_mcp_http_client(headers=_headers())
    )
    async with Client(transport) as upstream:
        server = make_proxy_server(upstream, await fetch_instructions(upstream))
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    print(DEPRECATION, file=sys.stderr)
    anyio.run(_serve)


if __name__ == "__main__":
    main()
