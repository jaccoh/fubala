"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from starlette.routing import Route

from . import __version__
from .auth import authorized
from .config import Settings, get_settings
from .db import init_db, make_engine
from .looplag import watch_loop_lag
from .mcp_tools import build_mcp_server, streamable_http_asgi
from .providers import LiteLLMGateway, ProviderGateway
from .registry import ProviderRegistry
from .routers import auth as auth_router
from .routers import chat, health, models, ping, providers, stories
from .routers import epics as epics_router
from .routers import projects as projects_router
from .routers import settings as settings_router
from .runner import StoryRunner
from .settings_store import get_api_token, load_settings

logger = logging.getLogger(__name__)

# the CLI-host mismatch warning must fire once per process, even if
# create_app() runs twice
_HOST_MISMATCH_WARNED = False


def _cli_host_flag(argv: list[str]) -> str | None:
    """The value of `--host X` / `--host=X` in argv, if any."""
    for i, arg in enumerate(argv[1:]):
        if arg == "--host" and i + 2 < len(argv):
            return argv[i + 2]
        if arg.startswith("--host="):
            return arg.split("=", 1)[1]
    return None


def _warn_cli_host_mismatch() -> None:
    """One WARNING when a plain uvicorn start binds a host that differs from
    Settings.host — a self-update restart lands on Settings.host/port, so the
    operator should know the two diverge before relying on a bookmark."""
    global _HOST_MISMATCH_WARNED
    if _HOST_MISMATCH_WARNED:
        return
    cli_host = _cli_host_flag(sys.argv)
    if cli_host is None:
        return
    settings_host = get_settings().host
    if cli_host == settings_host:
        _HOST_MISMATCH_WARNED = True
        return
    _HOST_MISMATCH_WARNED = True
    settings = get_settings()
    logger.warning(
        "uvicorn is binding --host %s but FUBALA_HOST (Settings.host) is %s: "
        "a self-update restart will come back on %s:%s, not on this CLI host "
        "— FUBALA_HOST/FUBALA_PORT in backend/.env are the single source for "
        "both `python -m app` and every self-update restart. Anything "
        "bookmarked on %s will break after a self-update restart; start with "
        "`cd backend && uv run python -m app` to keep the two in sync.",
        cli_host,
        settings_host,
        settings_host,
        settings.port,
        cli_host,
    )


class ApiAuthMiddleware:
    """Token wall for `/api/v1/*` and the MCP endpoint `/mcp` — pure ASGI, on
    purpose.

    `BaseHTTPMiddleware` buffers the response body, which breaks the SSE
    streams in `chat.py` (and the story-progress stream): those endpoints
    need the app's own `send` handed to them untouched. So this class wraps
    the app the old-fashioned way and only ever reads the scope, never the
    body — and it never logs headers, because a rejected `Authorization`
    header is itself a secret.
    """

    # exact-match: /health/live is a distinct path and must be listed too, or
    # the kubelet's liveness probe (no cookie) would 401 and restart the pod
    EXEMPT = ("/api/v1/health", "/api/v1/health/live")

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        guarded = path.startswith("/api/v1/") or path == "/mcp" or path.startswith("/mcp/")
        if not guarded or path in self.EXEMPT:
            await self.app(scope, receive, send)
            return
        engine = scope.get("app").state.engine if scope.get("app") else None
        if engine is None:  # not one of our requests; nothing to enforce with
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        if authorized(
            scope.get("headers") or [],
            client[0] if client else None,
            load_settings(engine),
            get_api_token(engine),
        ):
            await self.app(scope, receive, send)
            return
        await self._deny(send)

    @staticmethod
    async def _deny(send) -> None:
        body = b'{"detail": "authentication required"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def create_app(
    *,
    gateway: ProviderGateway | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings)
    registry = ProviderRegistry(engine)
    _warn_cli_host_mismatch()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(engine)
        _migrate(engine)
        registry.seed_if_empty()
        runner = StoryRunner(engine, app.state.gateway, registry=registry)
        app.state.story_runner = runner
        await runner.start()
        lag_task = asyncio.create_task(watch_loop_lag())
        try:
            # the MCP session manager must stay open for the whole app lifespan
            async with app.state.mcp_session_manager.run():
                yield
        finally:
            lag_task.cancel()
            await runner.stop()

    app = FastAPI(title="Fubala", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.registry = registry
    app.state.gateway = gateway or LiteLLMGateway(
        registry,
        max_output_tokens=settings.max_output_tokens,
        stream_idle_timeout=settings.stream_idle_timeout_seconds,
    )

    app.add_middleware(ApiAuthMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for r in (
        health.router,
        ping.router,
        models.router,
        providers.router,
        epics_router.router,
        projects_router.router,
        projects_router.permissions_router,
        settings_router.router,
        auth_router.router,
        stories.router,
        chat.router,
    ):
        app.include_router(r, prefix="/api/v1")

    # MCP over streamable HTTP at exactly /mcp (a Starlette Route, not a Mount,
    # so there is no 307 to /mcp/); registered before the SPA catch-all and
    # behind ApiAuthMiddleware. One server per app, tools bound in-process.
    mcp_server = build_mcp_server(app)
    app.state.mcp_session_manager = streamable_http_asgi(mcp_server)
    app.router.routes.append(
        Route("/mcp", endpoint=StreamableHTTPASGIApp(app.state.mcp_session_manager))
    )

    # serve the built UI when present: one process, one port — no dev server
    # to babysit (vite remains the hot-reload loop during development)
    dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    if dist.is_dir() and (dist / "index.html").is_file():
        from fastapi.responses import FileResponse

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            file = (dist / full_path).resolve()
            if full_path and file.is_file() and dist in file.parents:
                return FileResponse(file)
            return FileResponse(dist / "index.html")

    return app


app = create_app()


def _migrate(engine) -> None:
    """Inline data migrations (SQLite has no ALTER-friendly schema tooling here)."""
    from sqlalchemy import inspect, text

    # Inside the migration transaction the schema must be read through THAT
    # transaction's connection: a second pooled connection blocks on the lock
    # the transaction holds once it has run an ALTER/UPDATE ("database is
    # locked" on the live DB, image 3077, 24 Oct). Fresh test databases never
    # hit it -- create_all makes every column, so no ALTER runs in here.
    schema_conn = None

    def columns(table: str) -> dict[str, dict]:
        return {c["name"]: c for c in inspect(schema_conn or engine).get_columns(table)}

    story_cols = columns("stories")

    # v0.2: AI reviewer fields + review split
    with engine.begin() as conn:
        schema_conn = conn
        if "ai_review" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN ai_review TEXT"))
        if "ai_verdict" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN ai_verdict TEXT"))
        conn.execute(text("UPDATE stories SET status='human_review' WHERE status='review'"))
        if "skill" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN skill TEXT"))
        # slot fairness: when the story last entered the queue (NULL on
        # legacy rows — the comparator falls back to updated_at)
        if "queued_at" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN queued_at DATETIME"))
        # queue priority (normal|urgent); legacy rows read the default
        if "priority" not in story_cols:
            conn.execute(
                text("ALTER TABLE stories ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'")
            )
        # v0.4: execution stage — worktree base commit
        if "base_ref" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN base_ref TEXT"))
        # code-gate reject marker: branch head at the moment a human rejected
        # reviewed code, so the rerun can tell a no-op coder round from a real
        # one (same additive pattern as spec_rejects above)
        if "code_reject_head" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN code_reject_head TEXT"))
        # v0.6: self-improvement — verify stage result
        if "verify_result" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN verify_result TEXT"))
        # v0.8: external executors attach outcome evidence to a story
        if "evidence" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN evidence TEXT"))
        # v0.5: project support
        if "project_id" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN project_id TEXT"))
        if "epic_id" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN epic_id TEXT"))
        if "depends_on" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN depends_on TEXT"))
        if "project_id" not in columns("chat_sessions"):
            conn.execute(text("ALTER TABLE chat_sessions ADD COLUMN project_id TEXT"))
        if "verify_command" not in columns("projects"):
            conn.execute(text("ALTER TABLE projects ADD COLUMN verify_command TEXT"))
        # per-project test command for the coder's run_tests tool (NULL = the
        # tool is not exposed for that project; no backfill needed)
        if "test_command" not in columns("projects"):
            conn.execute(text("ALTER TABLE projects ADD COLUMN test_command TEXT"))
        # v0.7: external execution — stories runnable by outside agents
        if "execution" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN execution TEXT"))
            # pre-existing rows are internal by definition: never NULL, so
            # the scheduler's NULL-safe filter can't strand an upgraded
            # story. v0.10 note: this stamp runs only when the column is
            # brand new — afterwards NULL means "inherit the project's
            # executor" and must survive restarts.
            conn.execute(
                text("UPDATE stories SET execution='internal' WHERE execution IS NULL")
            )
        if "claimed_by" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN claimed_by VARCHAR(100)"))
        if "claimed_at" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN claimed_at DATETIME"))
        # pre-existing rows are internal by definition: never NULL, so the
        # scheduler's NULL-safe filter can't strand an upgraded story.
        # v0.10: execution moved to the project — new stories inherit
        # (execution IS NULL); legacy rows keep their concrete value, which
        # reads as an explicit override and preserves current behavior.
        if "executor" not in columns("projects"):
            conn.execute(text("ALTER TABLE projects ADD COLUMN executor TEXT"))
        # provider usage-limit reset zone (NULL = the zone default:
        # Asia/Shanghai for zai*, else UTC — registry.limit_reset_zone)
        if "limit_reset_tz" not in columns("providers"):
            conn.execute(text("ALTER TABLE providers ADD COLUMN limit_reset_tz TEXT"))
        # backfill ONLY rows not migrated yet (executor IS NULL). Unguarded,
        # this ran on every boot and flipped a whole project to external as
        # soon as one of its stories was landed by an external agent (16 Oct);
        # the scheduler then silently skipped every inheriting story.
        conn.execute(
            text(
                "UPDATE projects SET executor='external' WHERE executor IS NULL AND id IN "
                "(SELECT DISTINCT project_id FROM stories WHERE execution='external')"
            )
        )
        conn.execute(text("UPDATE projects SET executor='internal' WHERE executor IS NULL"))
        # v0.9: story kinds — code | info | prep decide the approval gate
        if "kind" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN kind TEXT"))
        conn.execute(text("UPDATE stories SET kind='code' WHERE kind IS NULL"))
        # bounded reject loop: consecutive AI-review rejections of the spec,
        # plus a per-version verdict history. Both additive.
        if "spec_rejects" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN spec_rejects INTEGER"))
        conn.execute(text("UPDATE stories SET spec_rejects=0 WHERE spec_rejects IS NULL"))
        if "spec_history" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN spec_history TEXT"))
        # v0.11: per-project delivery — push the branch (optional PR) instead
        # of merging into the root checkout. Same nullable + backfill pattern
        # as executor above.
        project_cols = columns("projects")
        if "delivery" not in project_cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN delivery TEXT"))
        if "pr_api_base" not in project_cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN pr_api_base TEXT"))
        if "pr_web_base" not in project_cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN pr_web_base TEXT"))
        if "pr_repo" not in project_cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN pr_repo TEXT"))
        if "pr_base_branch" not in project_cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN pr_base_branch TEXT"))
        if "pr_token_file" not in project_cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN pr_token_file TEXT"))
        conn.execute(text("UPDATE projects SET delivery='merge' WHERE delivery IS NULL"))
        # v0.12: push+PR delivery — landing state with a PR to watch. All
        # additive and nullable; `landing` is never inferred from NULL, only
        # ever written by _approve_push after a successful PR create.
        if "pr_url" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN pr_url TEXT"))
        if "pr_index" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN pr_index INTEGER"))
        if "pushed_sha" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN pushed_sha TEXT"))
        if "land_state" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN land_state TEXT"))
        # v0.13: per-story tool budget for the coder (None = app default).
        # Additive and nullable like the landing columns above.
        if "max_tool_calls" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN max_tool_calls INTEGER"))
        # v0.14: headless CODE executor — A/B share on the project. Additive
        # and nullable; read paths resolve NULL as 0 (never assign).
        if "ab_headless_share" not in columns("projects"):
            conn.execute(
                text("ALTER TABLE projects ADD COLUMN ab_headless_share REAL")
            )
        conn.execute(
            text("UPDATE projects SET ab_headless_share=0 WHERE ab_headless_share IS NULL")
        )
        # v0.15: per-story gates (human | auto) + the project-level defaults
        # and check inputs. Stories are nullable but backfilled to 'human' —
        # an upgraded story behaves exactly as it did before the gates
        # existed, and the API stamps every new story with a concrete value.
        if "gate_spec" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN gate_spec TEXT"))
        if "gate_code" not in story_cols:
            conn.execute(text("ALTER TABLE stories ADD COLUMN gate_code TEXT"))
        if "human_rejected" not in story_cols:
            conn.execute(
                text("ALTER TABLE stories ADD COLUMN human_rejected INTEGER")
            )
        conn.execute(text("UPDATE stories SET gate_spec='human' WHERE gate_spec IS NULL"))
        conn.execute(text("UPDATE stories SET gate_code='human' WHERE gate_code IS NULL"))
        conn.execute(
            text("UPDATE stories SET human_rejected=0 WHERE human_rejected IS NULL")
        )
        project_cols = columns("projects")
        if "default_gate_spec" not in project_cols:
            conn.execute(
                text("ALTER TABLE projects ADD COLUMN default_gate_spec TEXT")
            )
        if "default_gate_code" not in project_cols:
            conn.execute(
                text("ALTER TABLE projects ADD COLUMN default_gate_code TEXT")
            )
        if "protected_globs" not in project_cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN protected_globs TEXT"))
        if "frozen_globs" not in project_cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN frozen_globs TEXT"))
        if "auto_max_diff_lines" not in project_cols:
            conn.execute(
                text("ALTER TABLE projects ADD COLUMN auto_max_diff_lines INTEGER")
            )
        conn.execute(
            text("UPDATE projects SET default_gate_spec='human' WHERE default_gate_spec IS NULL")
        )
        conn.execute(
            text("UPDATE projects SET default_gate_code='human' WHERE default_gate_code IS NULL")
        )
        conn.execute(
            text("UPDATE projects SET protected_globs='' WHERE protected_globs IS NULL")
        )
        conn.execute(text("UPDATE projects SET frozen_globs='' WHERE frozen_globs IS NULL"))
        conn.execute(
            text(
                "UPDATE projects SET auto_max_diff_lines=400 "
                "WHERE auto_max_diff_lines IS NULL"
            )
        )
        # v0.16: merge train — one batch PR per window for a project's
        # gate-approved stories. New table + additive nullable columns, with
        # the off/300/6 backfill for legacy rows. A story column too: the
        # poller needs to know which batch judges it. `train_batches` itself
        # is created by SQLModel.metadata.create_all (new table, no ALTER).
        if "train_batch_id" not in story_cols:
            conn.execute(
                text("ALTER TABLE stories ADD COLUMN train_batch_id TEXT")
            )
        if "merge_train" not in project_cols:
            conn.execute(
                text("ALTER TABLE projects ADD COLUMN merge_train BOOLEAN")
            )
        if "train_window_s" not in project_cols:
            conn.execute(
                text("ALTER TABLE projects ADD COLUMN train_window_s INTEGER")
            )
        if "train_max" not in project_cols:
            conn.execute(text("ALTER TABLE projects ADD COLUMN train_max INTEGER"))
        conn.execute(
            text("UPDATE projects SET merge_train=0 WHERE merge_train IS NULL")
        )
        conn.execute(
            text("UPDATE projects SET train_window_s=300 WHERE train_window_s IS NULL")
        )
        conn.execute(text("UPDATE projects SET train_max=6 WHERE train_max IS NULL"))
        for col, ddl in (
            ("train_paused", "BOOLEAN"),
            ("train_paused_by", "TEXT"),
            ("train_pause_reason", "TEXT"),
            ("train_paused_at", "DATETIME"),
        ):
            if col not in project_cols:
                conn.execute(text(f"ALTER TABLE projects ADD COLUMN {col} {ddl}"))
        conn.execute(
            text("UPDATE projects SET train_paused=0 WHERE train_paused IS NULL")
        )
        # project brief: budget + spec-prompt opt-in. Additive and nullable;
        # read paths resolve NULL as 8000 / off. The version table itself is
        # created by create_all.
        if "brief_token_budget" not in project_cols:
            conn.execute(
                text("ALTER TABLE projects ADD COLUMN brief_token_budget INTEGER")
            )
        if "brief_in_spec_prompt" not in project_cols:
            conn.execute(
                text("ALTER TABLE projects ADD COLUMN brief_in_spec_prompt BOOLEAN")
            )
        # code graph (MCP + impact report): additive, NULL = off
        if "code_graph_enabled" not in project_cols:
            conn.execute(
                text("ALTER TABLE projects ADD COLUMN code_graph_enabled BOOLEAN")
            )
        # v0.17: the flush-time base of a batch. The done gate compares each
        # member's pushed_sha against it to tell "the member's own commit is
        # in main" from "the branch added nothing" (the empty-land incident).
        # Nullable: legacy rows keep NULL and the gate falls back to the old
        # member-ref check, so nothing already landing is stranded.
        if "base_sha" not in columns("train_batches"):
            conn.execute(
                text("ALTER TABLE train_batches ADD COLUMN base_sha TEXT")
            )
        if "ci_status" not in columns("train_batches"):
            conn.execute(
                text("ALTER TABLE train_batches ADD COLUMN ci_status TEXT")
            )
        if "not_before" not in columns("train_batches"):
            conn.execute(
                text("ALTER TABLE train_batches ADD COLUMN not_before DATETIME")
            )
        if "infra_retries" not in columns("train_batches"):
            conn.execute(
                text(
                    "ALTER TABLE train_batches "
                    "ADD COLUMN infra_retries INTEGER NOT NULL DEFAULT 0"
                )
            )
        if "failed_tests" not in columns("train_batches"):
            conn.execute(
                text("ALTER TABLE train_batches ADD COLUMN failed_tests TEXT")
            )

    # v0.3: stories.model became nullable (skill routing stamps it at pickup).
    # SQLite cannot drop NOT NULL, so rebuild the table when needed.
    if story_cols.get("model", {}).get("nullable") is False:
        _rebuild_stories(engine)


def _rebuild_stories(engine) -> None:
    """SQLite cannot DROP a NOT NULL constraint, so the table is rebuilt.

    This runs *after* the ALTER TABLE block above has added every missing
    column, so the rebuilt table must carry all of them over — a hardcoded
    pre-v0.4 column list would silently drop execution/claimed_by/
    claimed_at (and project/depends_on/base_ref/verify_result) from an
    upgraded DB right after they were added."""
    from sqlalchemy import inspect, text

    # everything the model declares; a legacy DB lacking one of these will
    # have just received it via ALTER above, so it is always present here
    wanted = [
        "id", "title", "description", "acceptance", "status", "model", "skill",
        "queued_at", "priority", "created_by", "execution", "gate_spec", "gate_code",
        "human_rejected", "kind", "claimed_by", "claimed_at", "spec",
        "spec_version", "spec_rejects", "spec_history", "base_ref", "code_reject_head",
        "verify_result", "evidence", "max_tool_calls", "ai_review",
        "ai_verdict", "approved_by", "error", "pr_url", "pr_index", "pushed_sha",
        "land_state", "train_batch_id", "project_id", "epic_id",
        "depends_on", "created_at", "updated_at",
    ]
    existing = {c["name"] for c in inspect(engine).get_columns("stories")}
    cols = [name for name in wanted if name in existing]

    new_schema = """
        CREATE TABLE stories_new (
            id VARCHAR NOT NULL PRIMARY KEY,
            title VARCHAR(200) NOT NULL,
            description TEXT NOT NULL,
            acceptance TEXT NOT NULL,
            status VARCHAR NOT NULL,
            model VARCHAR,
            skill VARCHAR,
            queued_at DATETIME,
            priority VARCHAR NOT NULL DEFAULT 'normal',
            created_by VARCHAR NOT NULL,
            execution VARCHAR,
            gate_spec VARCHAR,
            gate_code VARCHAR,
            human_rejected BOOLEAN,
            kind VARCHAR,
            claimed_by VARCHAR(100),
            claimed_at DATETIME,
            spec TEXT,
            spec_version INTEGER NOT NULL,
            spec_rejects INTEGER NOT NULL DEFAULT 0,
            spec_history TEXT,
            base_ref TEXT,
            code_reject_head TEXT,
            verify_result TEXT,
            max_tool_calls INTEGER,
            evidence TEXT,
            ai_review TEXT,
            ai_verdict VARCHAR,
            approved_by VARCHAR,
            error VARCHAR,
            pr_url TEXT,
            pr_index INTEGER,
            pushed_sha TEXT,
            land_state TEXT,
            train_batch_id TEXT,
            project_id TEXT,
            epic_id TEXT,
            depends_on TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        )
    """
    col_list = ", ".join(cols)
    with engine.begin() as conn:
        conn.execute(text(new_schema))
        conn.execute(
            text(f"INSERT INTO stories_new ({col_list}) SELECT {col_list} FROM stories")
        )
        conn.execute(text("DROP TABLE stories"))
        conn.execute(text("ALTER TABLE stories_new RENAME TO stories"))
        conn.execute(text("CREATE INDEX ix_stories_status ON stories (status)"))
        # the model indexes these; recreate whichever ones the rename lost
        for col in ("project_id", "epic_id", "execution"):
            if col in cols:
                conn.execute(
                    text(f"CREATE INDEX IF NOT EXISTS ix_stories_{col} ON stories ({col})")
                )
