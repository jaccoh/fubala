"""One start command: `cd backend && uv run python -m app`.

The bind comes from Settings — FUBALA_HOST/FUBALA_PORT in `backend/.env` or
the environment — which is the same source a self-update restart uses, so
the server always comes back on the interface and port it was started with.
Run this from `backend/` (the `-m` flag puts the cwd on sys.path).
"""

from __future__ import annotations

import sys

import uvicorn

from .config import get_settings


def main() -> int:
    settings = get_settings()
    # import string, not the module object: uvicorn spawns/loads it lazily,
    # exactly like `python -m uvicorn app.main:app` does
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
