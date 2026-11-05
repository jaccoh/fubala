# Spec: GET /api/v1/version

## Goal
`GET /api/v1/version` returns `200` with `{"version": "<app __version__>"}`, tagged `system`, and a pytest covers it; the full backend suite passes.

## Context
- `backend/app/__init__.py:3` defines `__version__ = "0.1.0"` — the single source of truth.
- `backend/app/routers/health.py` is the existing `system`-tagged router (`APIRouter(tags=["system"])`), mounted at prefix `/api/v1` in `backend/app/main.py` (line 68). `/health` there already returns the version via `HealthOut(status, version)`.
- Response schemas live in `backend/app/schemas.py` (e.g. `HealthOut` at line 78).
- Tests use the `client` fixture in `backend/tests/conftest.py` (tmp DB, `FakeGateway`). There is no existing health/system test file.
- This story is also a live probe of the self-update path (spec → code → review), so keep the diff minimal and boring.

## Implementation steps
1. In `backend/app/schemas.py`, add `VersionOut(BaseModel)` with a single required field `version: str`, next to `HealthOut`.
2. In `backend/app/routers/health.py`, add `@router.get("/version", response_model=VersionOut)` returning `VersionOut(version=__version__)`. Import `VersionOut`; `__version__` is already imported. No changes to `main.py` — the router is already mounted under `/api/v1`.
3. Create `backend/tests/test_version.py` with a test using the `client` fixture:
   - `r = client.get("/api/v1/version")`
   - assert `r.status_code == 200`
   - assert `r.json() == {"version": app.__version__}` (import `__version__` from `app`), which ties the assertion to the source of truth rather than the literal `"0.1.0"`.
4. Run `cd backend && uv run pytest` — all tests (75+ existing plus the new one) must pass.
5. No frontend or MCP changes.

## Risks & edge cases
- Do not duplicate the version string as a literal in the endpoint or test; both must import `__version__` so version bumps (self-update) flow through.
- Keep the response shape exactly `{"version": ...}` as specified — not the full `HealthOut`. Adding `status` would break the contract in the story.
- Don't add a second router or a new include in `main.py`; reusing the `system`-tagged health router keeps OpenAPI docs grouped and the diff minimal.
- If the coder model (zai/glm-5.3) reaches for `uvicorn`/network probes instead of `TestClient`, that's out of scope: the fixture-based client is sufficient and deterministic.

## Acceptance checks
- [ ] `GET /api/v1/version` (via test client) returns 200 with body `{"version": app.__version__}`.
- [ ] Endpoint appears under the `system` tag in `/api/v1/openapi.json` (`paths["/api/v1/version"].get.tags == ["system"]`).
- [ ] `backend/tests/test_version.py` exists and asserts both status code and body.
- [ ] `cd backend && uv run pytest` passes with zero failures.
- [ ] No changes outside `schemas.py`, `routers/health.py`, and the new test file.
