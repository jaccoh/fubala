"""API token management endpoints (generate / rotate / clear / session cookie).

The full token is returned by `POST /auth/token` **exactly once** — it is never
echoed by `GET /settings`, which only carries a 4-character hint. Rotation
invalidates every Bearer-configured client (MCP included) until their env is
updated; the session cookie rotates automatically because it is an HMAC of the
token.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ..auth import (
    SESSION_COOKIE,
    generate_token,
    is_loopback,
    session_value,
    verify,
)
from ..settings_store import get_api_token, set_api_token

router = APIRouter(prefix="/auth", tags=["auth"])


class TokenOut(BaseModel):
    token: str
    hint: str | None
    rotated: bool


class TokenStatus(BaseModel):
    hint: str | None


def _hint(token: str | None) -> str | None:
    return token[-4:] if token else None


@router.post("/token", response_model=TokenOut)
def rotate_token(request: Request) -> TokenOut:
    """Generate a new token, replacing any existing one (rotate)."""
    previous = get_api_token(request.app.state.engine)
    token = generate_token()
    set_api_token(request.app.state.engine, token)
    return TokenOut(token=token, hint=_hint(token), rotated=previous is not None)


@router.delete("/token", response_model=TokenStatus)
def delete_token(request: Request) -> TokenStatus:
    """Clear the stored token: every Bearer client stops working until a new
    token is generated. Loopback/session clients are unaffected."""
    set_api_token(request.app.state.engine, None)
    return TokenStatus(hint=None)


@router.get("/token", response_model=TokenStatus)
def token_status(request: Request) -> TokenStatus:
    """Token hint (last 4 chars) — never the token itself."""
    return TokenStatus(hint=_hint(get_api_token(request.app.state.engine)))


@router.post("/session")
def create_session(request: Request, response: Response) -> dict:
    """Mint the HttpOnly session cookie for *this* browser.

    Two ways in:

    * a loopback client (the local UI just works; needs a configured token,
      else 409), or
    * a remote client presenting a valid ``Authorization: Bearer`` token —
      this is the browser sign-in path: paste the token once, get the
      cookie, let the Bearer header go away.

    Anything else is 401. The cookie value is an HMAC of the API token, so it
    stays valid exactly as long as the token does. `Secure` is set only when
    the request arrived over HTTPS (``X-Forwarded-Proto: https`` behind the
    reverse proxy): LAN HTTP is a supported deployment; treat the LAN as the
    trust boundary. A forged ``X-Forwarded-Proto`` can at worst add `Secure`
    to the cookie — it never bypasses authentication.
    """
    secure = request.headers.get("x-forwarded-proto", "").lower() == "https"
    client = request.client
    if is_loopback(client.host if client else None):
        token = get_api_token(request.app.state.engine)
        if not token:
            raise HTTPException(status_code=409, detail="no API token configured")
        _set_session_cookie(response, token, secure)
        return {"ok": True, "cookie": SESSION_COOKIE}

    # Non-loopback: prove knowledge of the API token, get the cookie in return.
    authorization = request.headers.get("authorization", "")
    scheme, _, credential = authorization.partition(" ")
    bearer = credential.strip() if scheme.strip().lower() == "bearer" else ""
    stored = get_api_token(request.app.state.engine)
    # missing / malformed / invalid / unconfigured all look identical on
    # purpose — no oracle about which part was wrong (and the header value is
    # never logged: a submitted Bearer is itself a secret)
    if not verify(bearer, stored):
        raise HTTPException(status_code=401, detail="authentication required")
    _set_session_cookie(response, stored, secure)
    return {"ok": True, "cookie": SESSION_COOKIE}


def _set_session_cookie(response: Response, token: str, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_value(token),
        httponly=True,
        samesite="lax",
        secure=secure,
    )
