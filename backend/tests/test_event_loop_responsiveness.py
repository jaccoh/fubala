"""The event loop must never block on verify or git.

The server froze on 16 Oct: `_verify_and_fix` called the sync
`subprocess.run` inside `_run_verify_command` directly on the loop, so a slow
verify starved every request — /health, SSE, other stories.

These tests pin the fix:
1. a running verify leaves `/api/v1/health` responsive (red on the old code),
2. the long git calls on async runner paths (create / commit x2 / diff) are
   wrapped in `asyncio.to_thread`, so each blocks a thread, not the loop,
3. an `approve_story` merge (sync `def` endpoint) stays responsive — FastAPI
   runs sync endpoints in the worker threadpool, so the endpoint must NEVER be
   converted to `async def` (that would move the git merge back onto the loop),
4. `_run_verify_command` keeps its sync signature and its timeout kill.
"""

from __future__ import annotations

import asyncio
import inspect
import subprocess
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app import workspaces
from app.db import session_scope
from app.main import create_app
from app.models import Story, StoryStatus
from app.runner import StoryRunner, _run_verify_command


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return repo


def make_client(tmp_path: Path, monkeypatch) -> TestClient:
    """App on a tmp DB. Enter the context (which creates the tables) before
    touching settings or stories."""
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "loop.db"))
    return TestClient(create_app())


def apply_settings(client: TestClient, tmp_path: Path, *, verify_command: str) -> None:
    """Every field asserted: a 422 here would silently un-configure the test
    (no project root, no verify command) and green it for nothing."""
    res = client.patch(
        "/api/v1/settings",
        json={
            "story_poll_interval": 60,  # max allowed; keeps the poller out of the way
            "project_root": str(tmp_path / "project"),
            "verify_command": verify_command,
            "write_tools_enabled": True,  # _execute_story refuses to run without it
        },
    )
    assert res.status_code == 200, res.text


def _make_worktree(repo: Path, story_id: str) -> Path:
    """A plain directory standing in for the story worktree (verify only needs
    a cwd that exists)."""
    ws = repo / ".fubala-worktrees" / story_id
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "change.txt").write_text("x\n")
    return ws


async def _probe_health(app, bound: float = 0.5) -> httpx.Response:
    """One /health request through the app's own ASGI stack, timed.

    ASGITransport is in-process — there is no socket, so httpx's own timeout
    can never fire: a blocked loop simply parks this await until the blocker
    is done, and the (late) response still looks like a 200. The wall clock is
    therefore the only honest probe, so the response must arrive inside
    `bound` seconds of the request being issued."""
    started = time.monotonic()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        timeout=bound,
    ) as api:
        res = await api.get("/api/v1/health")
    elapsed = time.monotonic() - started
    assert elapsed < bound, (
        f"/health answered in {elapsed:.2f}s (> {bound}s) — the event loop is blocked"
    )
    return res


async def _coder_round(prompt: str) -> None:
    """No-op coder: the fix round is irrelevant to the loop measurement."""
    return None


# ---- 1. verify must leave the loop free ----------------------------------


def test_health_stays_responsive_during_verify(tmp_path, monkeypatch):
    """Red on unpatched runner.py:1340 — while a `sleep 3` verify runs, the
    loop must still answer /health well inside half a second (0.5 s bound vs
    3 s sleep = ~6x margin; the loop answers in ms once it is free)."""
    repo = init_repo(tmp_path)
    client = make_client(tmp_path, monkeypatch)

    with client:
        apply_settings(client, tmp_path, verify_command="sleep 3")
        story_id = client.post("/api/v1/stories", json={"title": "t"}).json()["id"]
        ws = _make_worktree(repo, story_id)
        runner: StoryRunner = client.app.state.story_runner

        async def scenario() -> None:
            verify = asyncio.create_task(
                runner._verify_and_fix(
                    story_id,
                    str(repo),
                    ws,
                    coder_round=_coder_round,
                    title="t",
                    spec=None,
                )
            )
            try:
                res = await _probe_health(client.app, bound=0.5)
                assert res.status_code == 200, res.text
            finally:
                await verify

        asyncio.run(scenario())


# ---- 2. long git calls on async runner paths -----------------------------


def test_runner_git_calls_stay_off_the_loop(tmp_path, monkeypatch):
    """`workspaces.create` (in `_execute_story`), `commit` (coder output in
    `_execute_story`, fix round in `_verify_and_fix`) and `diff` (in
    `_run_code_review`) all run on async paths. Each is made artificially slow
    here; while it runs, /health must still answer — which only holds if the
    runner awaits them via `asyncio.to_thread` rather than calling the sync
    functions on the loop."""
    repo = init_repo(tmp_path)
    client = make_client(tmp_path, monkeypatch)

    for name in ("create", "commit", "diff"):
        real = getattr(workspaces, name)

        def slowed(*args, _real=real, **kwargs):
            time.sleep(2)
            return _real(*args, **kwargs)

        monkeypatch.setattr(workspaces, name, slowed)

    with client:
        apply_settings(client, tmp_path, verify_command="false")
        created = client.post("/api/v1/stories", json={"title": "t"}).json()
        story_id = created["id"]
        runner: StoryRunner = client.app.state.story_runner
        ws = _make_worktree(repo, story_id)

        with session_scope(client.app.state.engine) as db:
            story = db.get(Story, story_id)
            story.status = StoryStatus.EXECUTING
            story.spec = "## Goal\ndo the thing\n"
            db.add(story)

        async def scenario() -> None:
            async def probe_while(task) -> None:
                try:
                    res = await _probe_health(client.app, bound=0.5)
                finally:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                assert res.status_code == 200, res.text

            # create (in _execute_story) — the agent call later fails (no
            # gateway configured), but the create call itself must not block
            await probe_while(asyncio.create_task(runner._execute_story(story_id)))

            # commit (fix round) inside _verify_and_fix
            await probe_while(
                asyncio.create_task(
                    runner._verify_and_fix(
                        story_id,
                        str(repo),
                        ws,
                        coder_round=_coder_round,
                        title="t",
                        spec=None,
                    )
                )
            )

            # diff inside _run_code_review
            with session_scope(client.app.state.engine) as db:
                story = db.get(Story, story_id)
                story.status = StoryStatus.AI_REVIEW
                story.base_ref = workspaces.head(str(repo))
                db.add(story)
            await probe_while(asyncio.create_task(runner._run_code_review(story_id)))

        asyncio.run(scenario())


# ---- 3. approve stays a sync endpoint (threadpool) -----------------------


def test_sync_approve_endpoint_stays_off_the_loop(tmp_path, monkeypatch):
    """Invariant: `approve_story` is a SYNC `def` endpoint, so Starlette runs
    merge/discard in its worker threadpool — off the loop for free. This test
    fails if anyone converts the endpoint to `async def` without wrapping the
    git calls: the 2 s merge below would then block /health past 0.5 s."""
    repo = init_repo(tmp_path)
    client = make_client(tmp_path, monkeypatch)

    real_merge = workspaces.merge

    def slow_merge(*args, **kwargs):
        time.sleep(2)
        return real_merge(*args, **kwargs)

    monkeypatch.setattr(workspaces, "merge", slow_merge)

    with client:
        apply_settings(client, tmp_path, verify_command="true")
        created = client.post("/api/v1/stories", json={"title": "t"}).json()
        story_id = created["id"]
        # the story needs real work to merge: create the worktree + a commit
        ws, base, _new = workspaces.create(str(repo), story_id)
        (ws / "feature.py").write_text("print('new')\n")
        workspaces.commit(str(repo), story_id, "story: feature")

        with session_scope(client.app.state.engine) as db:
            story = db.get(Story, story_id)
            story.status = StoryStatus.HUMAN_REVIEW
            story.spec = "## Goal\ndummy\n"
            story.base_ref = base
            db.add(story)

        async def scenario() -> None:
            approve = asyncio.create_task(_post_approve(client.app, story_id))
            await asyncio.sleep(0.1)  # let the request reach the merge
            res = await _probe_health(client.app, bound=0.5)
            assert res.status_code == 200, res.text
            merged = await approve
            assert merged.status_code == 200, merged.text

        asyncio.run(scenario())


async def _post_approve(app, story_id: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        timeout=30,
    ) as api:
        return await api.post(
            f"/api/v1/stories/{story_id}/approve", json={"by": "t"}
        )


# ---- 4. _run_verify_command itself --------------------------------------


def test_verify_command_timeout_kills_child(tmp_path):
    """rc 124 + the timeout message, and the `/bin/sh` child does not outlive
    the timeout."""
    repo = init_repo(tmp_path)
    ws = repo / ".fubala-worktrees" / "s"
    ws.mkdir(parents=True)

    start = time.monotonic()
    rc, output = _run_verify_command(ws, "sleep 5", 0.5, str(repo))
    elapsed = time.monotonic() - start

    assert rc == 124
    assert "verify timed out after 0.5s" in output
    # not a wait: upper bound proving the 0.5s verify timeout killed the child
    assert elapsed < 5, f"the child outlived the timeout ({elapsed:.1f}s)"


def test_run_verify_command_stays_sync_with_tested_rc_mapping(tmp_path):
    """`_run_verify_command` remains a sync callable with the rc/output
    semantics test_warm_verify.py depends on (it must NOT become async)."""
    repo = init_repo(tmp_path)
    ws = repo / ".fubala-worktrees" / "s2"
    ws.mkdir(parents=True)

    assert not inspect.iscoroutinefunction(_run_verify_command)
    rc, out = _run_verify_command(ws, "echo hi && exit 3", 10, str(repo))
    assert rc == 3
    assert "hi" in out
