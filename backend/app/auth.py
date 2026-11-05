"""API token authentication for non-local clients.

When ``api_auth_enabled`` is on, every ``/api/v1/*`` request except
``/api/v1/health`` must present either:

* a loopback client address (the local UI just works),
* a valid ``Authorization: Bearer fubala_…`` token, or
* a valid ``fubala_session`` cookie (HMAC of the token, minted from loopback).

The token itself lives in its own ``AppSetting`` row ("api_token") so it can
never leak into the merged settings dict that ``GET /api/v1/settings`` echoes.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

# key of the AppSetting row that stores the raw token (NOT in DEFAULTS)
TOKEN_SETTINGS_KEY = "api_token"
SESSION_COOKIE = "fubala_session"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def generate_token() -> str:
    """New opaque bearer token; shown to the operator exactly once."""
    return "fubal_" + secrets.token_urlsafe(32)


def verify(token: str, expected: str | None) -> bool:
    """Constant-time comparison; empty/None never matches."""
    if not token or not expected:
        return False
    return secrets.compare_digest(token, expected)


def is_loopback(client_host: str | None) -> bool:
    """True for loopback addresses. X-Forwarded-For is deliberately ignored."""
    if not client_host:
        return False
    host = client_host.split("%", 1)[0]  # strip IPv6 zone id (::1%lo0)
    return host.strip("[]").lower() in LOOPBACK_HOSTS


def session_value(token: str) -> str:
    """Cookie value bound to the token: HMAC of the token.

    Rotating the token changes the HMAC, so sessions cannot outlive their
    token. The secret is derived from the token itself — there is nothing
    else server-side to derive from, and knowing the cookie value does not
    reveal the token (HMAC one way).
    """
    key = hashlib.sha256(b"fubala-session-v1").digest()
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()


def verify_session_cookie(cookie_value: str | None, token: str | None) -> bool:
    if not cookie_value or not token:
        return False
    return secrets.compare_digest(cookie_value, session_value(token))


def _bearer_from_headers(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Extract the Bearer credential from raw ASGI headers (no req object)."""
    for name, value in headers:
        if name.lower() != b"authorization":
            continue
        try:
            text = value.decode("latin-1")
        except UnicodeDecodeError:  # pragma: no cover - defensive
            return None
        scheme, _, credential = text.partition(" ")
        if scheme.strip().lower() == "bearer" and credential.strip():
            return credential.strip()
    return None


def _cookie_from_headers(
    headers: list[tuple[bytes, bytes]], wanted: str
) -> str | None:
    for name, value in headers:
        if name.lower() != b"cookie":
            continue
        try:
            text = value.decode("latin-1")
        except UnicodeDecodeError:  # pragma: no cover - defensive
            return None
        for part in text.split(";"):
            key, _, val = part.strip().partition("=")
            if key == wanted:
                return val.strip()
    return None


def authorized(
    scope_headers: list[tuple[bytes, bytes]],
    client_host: str | None,
    settings: dict[str, Any],
    token_row: str | None,
) -> bool:
    """Decide one request. Pure: no DB, no I/O — the caller supplies state."""
    if not settings.get("api_auth_enabled", False):
        return True
    if is_loopback(client_host):
        return True
    if verify(_bearer_from_headers(scope_headers) or "", token_row):
        return True
    cookie = _cookie_from_headers(scope_headers, SESSION_COOKIE)
    return verify_session_cookie(cookie, token_row)
