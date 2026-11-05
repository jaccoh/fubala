"""API token authentication: middleware, endpoints, hints, redaction, MCP.

TestClient's peer address is the literal string "testclient" — i.e. NOT
loopback — which makes it the perfect stand-in for a remote client. Loopback
cases are covered by wrapping the app in a tiny ASGI shim that rewrites
`scope["client"]` before the app sees it.

Both clients share ONE app (and therefore one engine): a token minted through
the loopback client must be exactly the token the remote client is judged
against.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import mcp
from app.auth import (
    generate_token,
    is_loopback,
    session_value,
    verify,
    verify_session_cookie,
)
from app.guardrails import redact_secrets
from app.main import create_app
from app.settings_store import get_api_token, set_api_token


class LoopbackShim:
    """ASGI wrapper that forces the peer address to 127.0.0.1."""

    def __init__(self, app, host: str = "127.0.0.1"):
        self.app = app
        self.host = host

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope["client"] = (self.host, 54321)
        await self.app(scope, receive, send)


class Api:
    """One app, one DB, two peer-address views of it."""

    def __init__(self, application) -> None:
        self.app = application
        self.remote = TestClient(application)  # peer address: "testclient"
        self.local = TestClient(LoopbackShim(application))

    def __enter__(self):
        # only one client may enter the lifespan: the runner must not start twice
        self.local.__enter__()
        return self

    def __exit__(self, *exc):
        return self.local.__exit__(*exc)


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "auth.db"))
    with Api(create_app()) as a:
        yield a


def _mint(c: TestClient) -> str:
    res = c.post("/api/v1/auth/token")
    assert res.status_code == 200, res.text
    return res.json()["token"]


def _enable(c: TestClient, **extra) -> None:
    res = c.patch("/api/v1/settings", json={"api_auth_enabled": True, **extra})
    assert res.status_code == 200, res.text
    assert res.json()["api_auth_enabled"] is True


# ---- unit: token primitives -------------------------------------------


def test_generate_token_shape():
    token = generate_token()
    assert token.startswith("fubal_")
    assert len(token) > 20
    assert token != generate_token()


def test_verify_constant_time_compare():
    token = generate_token()
    assert verify(token, token)
    assert not verify(token + "x", token)
    assert not verify(token, None)
    assert not verify("", token)


def test_is_loopback():
    for host in ("127.0.0.1", "::1", "localhost", "[::1]", "::1%lo0", "LOCALHOST"):
        assert is_loopback(host), host
    # no peer info at all is NOT trusted: a wall that lets empty through lets
    # any proxy that drops client info through
    for host in (None, "", "192.168.1.5", "10.0.0.1", "testclient", "example.com"):
        assert not is_loopback(host), host


# ---- default-off: nothing changes -------------------------------------


def test_auth_disabled_by_default(api):
    assert api.remote.get("/api/v1/stories").status_code == 200
    settings = api.remote.get("/api/v1/settings").json()
    assert settings["api_auth_enabled"] is False


# ---- enabled + non-loopback, no token --------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/stories"),
        ("POST", "/api/v1/stories"),
        ("GET", "/api/v1/settings"),
        ("PATCH", "/api/v1/settings"),
        ("GET", "/api/v1/chat/sessions"),
        ("POST", "/api/v1/chat/sessions"),
        ("GET", "/api/v1/models"),
    ],
)
def test_enabled_blocks_remote_without_token(api, method, path):
    _enable(api.remote)
    body = {"title": "x"} if method == "POST" else (
        {"api_auth_enabled": True} if method == "PATCH" else None
    )
    res = api.remote.request(method, path, json=body)
    assert res.status_code == 401
    assert res.json() == {"detail": "authentication required"}


def test_enabled_health_stays_open(api):
    _enable(api.remote)
    assert api.remote.get("/api/v1/health").status_code == 200
    # the kubelet's liveness probe is a remote, credential-less client: a 401
    # here would restart the pod loop-side
    assert api.remote.get("/api/v1/health/live").status_code == 200


# ---- bearer ----------------------------------------------------------


def test_valid_bearer_roundtrip(api):
    _enable(api.local)
    headers = {"Authorization": f"Bearer {_mint(api.local)}"}
    # representative read + write
    assert api.remote.get("/api/v1/stories", headers=headers).status_code == 200
    created = api.remote.post(
        "/api/v1/stories", json={"title": "auth story"}, headers=headers
    )
    assert created.status_code in (200, 201), created.text
    sid = created.json()["id"]
    patched = api.remote.patch(
        f"/api/v1/stories/{sid}", json={"title": "renamed"}, headers=headers
    )
    assert patched.status_code == 200, patched.text
    assert api.remote.get("/api/v1/settings", headers=headers).status_code == 200


def test_wrong_token_rejected(api):
    _enable(api.local)
    _mint(api.local)
    res = api.remote.get(
        "/api/v1/stories", headers={"Authorization": "Bearer fubal_nope"}
    )
    assert res.status_code == 401


def test_bearer_required_when_no_token_configured(api):
    _enable(api.local)
    res = api.remote.get(
        "/api/v1/stories", headers={"Authorization": "Bearer fubal_nothing"}
    )
    assert res.status_code == 401


# ---- loopback trust ---------------------------------------------------


def test_enabled_loopback_unaffected(api):
    _enable(api.local)
    assert api.local.get("/api/v1/stories").status_code == 200
    assert api.local.patch(
        "/api/v1/settings", json={"story_poll_interval": 0.05}
    ).status_code == 200


# ---- session cookie ---------------------------------------------------


def test_session_minted_from_loopback_authorizes_remote(api):
    _enable(api.local)
    token = _mint(api.local)
    res = api.local.post("/api/v1/auth/session")
    assert res.status_code == 200, res.text
    cookie = api.local.cookies.get("fubala_session")
    assert cookie == session_value(token)
    assert verify_session_cookie(cookie, token)

    # the same cookie now vouches for a non-loopback-flagged request
    assert api.remote.get(
        "/api/v1/stories", cookies={"fubala_session": cookie}
    ).status_code == 200


def test_session_minted_from_remote_bearer_authorizes_remote(api):
    """Browser sign-in: paste the token once, get the cookie, drop the header."""
    _enable(api.local)
    token = _mint(api.local)
    res = api.remote.post(
        "/api/v1/auth/session", headers={"Authorization": f"Bearer {token}"}
    )
    assert res.status_code == 200, res.text
    cookie = res.cookies.get("fubala_session")
    assert cookie == session_value(token)
    assert verify_session_cookie(cookie, token)

    # the cookie alone — no Bearer header — clears the wall
    assert api.remote.get(
        "/api/v1/stories", cookies={"fubala_session": cookie}
    ).status_code == 200


def test_session_rejects_invalid_bearer(api):
    """Remote + wrong token: 401, and never even a Set-Cookie header."""
    _enable(api.local)
    _mint(api.local)
    res = api.remote.post(
        "/api/v1/auth/session", headers={"Authorization": "Bearer fubal_nope"}
    )
    assert res.status_code == 401
    assert res.json() == {"detail": "authentication required"}
    assert "set-cookie" not in {k.lower() for k in res.headers.keys()}


def test_session_rejects_remote_without_bearer(api):
    _enable(api.local)
    _mint(api.local)
    res = api.remote.post("/api/v1/auth/session")
    assert res.status_code == 401
    assert res.json() == {"detail": "authentication required"}
    assert "set-cookie" not in {k.lower() for k in res.headers.keys()}


def test_session_remote_bearer_without_configured_token(api):
    """Auth on but no token generated: nothing to verify against, so 401."""
    _enable(api.local)
    res = api.remote.post(
        "/api/v1/auth/session", headers={"Authorization": "Bearer fubal_nope"}
    )
    assert res.status_code == 401
    assert "set-cookie" not in {k.lower() for k in res.headers.keys()}


def test_session_cookie_secure_iff_https(api):
    """`Secure` follows X-Forwarded-Proto; loopback has no header → plain."""
    _enable(api.local)
    token = _mint(api.local)

    res = api.remote.post(
        "/api/v1/auth/session",
        headers={"Authorization": f"Bearer {token}", "X-Forwarded-Proto": "https"},
    )
    assert res.status_code == 200, res.text
    cookie_header = res.headers.get("set-cookie", "")
    assert "secure" in cookie_header.lower()
    assert "httponly" in cookie_header.lower()
    assert "samesite=lax" in cookie_header.lower()

    res = api.remote.post(
        "/api/v1/auth/session", headers={"Authorization": f"Bearer {token}"}
    )
    assert res.status_code == 200, res.text
    assert "secure" not in res.headers.get("set-cookie", "").lower()
    assert "httponly" in res.headers.get("set-cookie", "").lower()

    # loopback mint: TestClient sends no X-Forwarded-Proto → not Secure
    res = api.local.post("/api/v1/auth/session")
    assert res.status_code == 200, res.text
    assert "secure" not in res.headers.get("set-cookie", "").lower()

    # ... and with the header it IS Secure, on the loopback branch too
    res = api.local.post("/api/v1/auth/session", headers={"X-Forwarded-Proto": "https"})
    assert res.status_code == 200, res.text
    assert "secure" in res.headers.get("set-cookie", "").lower()


def test_session_requires_configured_token(api):
    _enable(api.local)
    assert api.local.post("/api/v1/auth/session").status_code == 409


def test_rotation_invalidates_old_cookie_and_token(api):
    _enable(api.local)
    old = _mint(api.local)
    cookie = api.local.post("/api/v1/auth/session").cookies["fubala_session"]
    assert api.remote.get(
        "/api/v1/stories", headers={"Authorization": f"Bearer {old}"}
    ).status_code == 200

    new = _mint(api.local)
    assert new != old
    # old token dead, new token live, old cookie dead
    assert api.remote.get(
        "/api/v1/stories", headers={"Authorization": f"Bearer {old}"}
    ).status_code == 401
    assert api.remote.get(
        "/api/v1/stories", headers={"Authorization": f"Bearer {new}"}
    ).status_code == 200
    assert api.remote.get(
        "/api/v1/stories", cookies={"fubala_session": cookie}
    ).status_code == 401


def test_delete_token(api):
    _enable(api.local)
    token = _mint(api.local)
    # revocation is an admin action: performed from the local UI
    assert api.local.delete("/api/v1/auth/token").status_code in (200, 204)
    assert api.remote.get(
        "/api/v1/stories", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 401
    assert get_api_token(api.app.state.engine) is None


# ---- hints: never the token ------------------------------------------


def test_settings_expose_hint_only(api):
    _enable(api.local)
    token = _mint(api.local)
    res = api.remote.get(
        "/api/v1/settings", headers={"Authorization": f"Bearer {token}"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["api_token_hint"] == token[-4:]
    # the token itself must appear nowhere in the payload
    assert token not in res.text


def test_token_hint_none_when_unset(api):
    assert api.remote.get("/api/v1/settings").json()["api_token_hint"] is None


# ---- guardrails -------------------------------------------------------


def test_redact_secrets_scrubs_fubala_token():
    token = "fubal_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"  # 32 chars after prefix
    assert redact_secrets(f"Bearer {token}") == "Bearer [REDACTED:fubala-token]"
    assert redact_secrets(token) == "[REDACTED:fubala-token]"


# ---- MCP stdio proxy headers ----------------------------------------


def test_mcp_headers_empty_without_token(monkeypatch):
    monkeypatch.delenv("FUBALA_API_TOKEN", raising=False)
    assert mcp._headers() == {}


def test_mcp_headers_bearer_with_token(monkeypatch):
    monkeypatch.setenv("FUBALA_API_TOKEN", "fubal_testtesttesttesttest")
    assert mcp._headers() == {"Authorization": "Bearer fubal_testtesttesttesttest"}


# ---- storage isolation ------------------------------------------------


def test_token_never_in_merged_settings(api):
    token = _mint(api.local)
    from app.settings_store import load_settings

    engine = api.app.state.engine
    assert get_api_token(engine) == token
    merged = load_settings(engine)
    assert "api_token" not in merged
    assert token not in merged.values()


def test_set_and_clear_token_row(tmp_path, monkeypatch):
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "t.db"))
    app = create_app()
    from app.db import init_db

    engine = app.state.engine
    init_db(engine)
    assert get_api_token(engine) is None
    set_api_token(engine, "fubal_row_test_1234567890")
    assert get_api_token(engine) == "fubal_row_test_1234567890"
    set_api_token(engine, None)
    assert get_api_token(engine) is None
