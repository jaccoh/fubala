"""A hanging Gitea must never block the event loop.

The 23 Oct hang: the scheduler tick called sync urllib (app/pr.py) on the
loop, so one slow Gitea froze /health, SSE and every other request. The land,
reconcile and train sweeps now run on a dedicated thread pool under a cap.

The fake Gitea sleeps 5 s per request while PR_HTTP_TIMEOUT is 3 s: with the
sweep inline, every poll blocks the loop for ~3 s, which breaks both the 1 s
health bound and the loop-lag watchdog assertion.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import pr as pr_module
from app import runner as runner_module
from app import train as train_module
from app.events import EventBus
from app.looplag import watch_loop_lag
from app.main import create_app
from app.models import Story, StoryStatus
from app.runner import StoryRunner


class _SlowGitea(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        time.sleep(5)
        try:
            self.send_response(200)
            self.end_headers()
        except OSError:
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def slow_gitea():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowGitea)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_hanging_gitea_never_blocks_the_loop(tmp_path, monkeypatch, slow_gitea, caplog):
    monkeypatch.setattr(pr_module, "PR_HTTP_TIMEOUT", 3.0)
    monkeypatch.setattr(pr_module, "PR_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(runner_module, "LAND_POLL_MIN_INTERVAL", 0.0)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "block.db"))
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    caplog.set_level(logging.WARNING)

    with TestClient(create_app()) as client:
        client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
        project = client.post(
            "/api/v1/projects",
            json={
                "name": "p",
                "root": str(tmp_path),
                "delivery": "push",
                "pr_api_base": f"http://127.0.0.1:{slow_gitea.server_address[1]}",
                "pr_repo": "acme/app",
                "pr_base_branch": "main",
                "pr_token_file": str(token_file),
            },
        ).json()
        story = client.post(
            "/api/v1/stories", json={"title": "landing", "project_id": project["id"]}
        ).json()
        with Session(client.app.state.engine) as db:
            row = db.get(Story, story["id"])
            row.status = StoryStatus.LANDING
            row.pr_index = 7
            db.add(row)
            db.commit()

        result: dict = {"health": [], "stories": []}

        def poll_api() -> None:
            # a blocked loop must fail the test, not hang it: this thread is
            # joined with a timeout below
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                started = time.monotonic()
                result["health"].append(
                    (client.get("/api/v1/health").status_code, time.monotonic() - started)
                )
                result["stories"].append(client.get("/api/v1/stories").status_code)
                warned = [r for r in caplog.records if "land poll for story" in r.getMessage()]
                if len(warned) >= 2:
                    return
                time.sleep(0.2)

        worker = threading.Thread(target=poll_api, daemon=True)
        worker.start()
        worker.join(timeout=40)
        assert not worker.is_alive(), "API requests hung: the event loop is blocked"

    assert result["health"], "no probe completed"
    for status, elapsed in result["health"]:
        assert status == 200
        assert elapsed < 1.0, f"/health took {elapsed:.2f}s while Gitea hangs"
    assert set(result["stories"]) == {200}
    warned = [r for r in caplog.records if "land poll for story" in r.getMessage()]
    assert len(warned) >= 2, "the poll must time out and retry on the next tick"
    lagged = [
        r for r in caplog.records
        if r.name == "app.looplag" and "event loop lag" in r.getMessage()
    ]
    assert not lagged, [r.getMessage() for r in lagged]


def test_loop_lag_watchdog_logs_a_blocked_loop(caplog):
    caplog.set_level(logging.WARNING, logger="app.looplag")

    async def scenario() -> None:
        task = asyncio.create_task(watch_loop_lag())
        await asyncio.sleep(0.7)
        time.sleep(2.5)  # blocks the loop
        await asyncio.sleep(0.7)
        task.cancel()

    asyncio.run(scenario())
    assert any("event loop lag" in r.getMessage() for r in caplog.records)


def test_eventbus_publish_from_a_thread_hops_onto_the_loop(monkeypatch):
    async def scenario() -> None:
        bus = EventBus()
        queue = await bus.subscribe()
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()

        hops: list[tuple[int, object]] = []
        real_hop = loop.call_soon_threadsafe

        def recording_hop(callback, *args, **kwargs):
            hops.append((threading.get_ident(), callback))
            return real_hop(callback, *args, **kwargs)

        monkeypatch.setattr(loop, "call_soon_threadsafe", recording_hop)

        put_threads: list[int] = []
        real_put = queue.put_nowait

        def recording_put(item):
            put_threads.append(threading.get_ident())
            return real_put(item)

        monkeypatch.setattr(queue, "put_nowait", recording_put)

        publisher = threading.Thread(target=bus.publish, args=("story", {"n": 1}))
        publisher.start()
        await asyncio.to_thread(publisher.join)
        await asyncio.sleep(0)

        foreign = [h for h in hops if h[0] == publisher.ident]
        assert len(foreign) == 1 and foreign[0][1] is recording_put
        assert put_threads == [loop_thread], "the queue was touched off the loop thread"
        assert '"kind": "story"' in queue.get_nowait()

    asyncio.run(scenario())


def _runner(client: TestClient) -> StoryRunner:
    return StoryRunner(
        client.app.state.engine,
        client.app.state.gateway,
        registry=client.app.state.registry,
    )


def test_reconcile_sweep_runs_off_the_loop(client, monkeypatch):
    with Session(client.app.state.engine) as db:
        db.add(Story(id="failedone", title="f", status=StoryStatus.FAILED))
        db.commit()
    seen: list[tuple[int, bool]] = []

    def fake_reconcile(db, story):
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        seen.append((threading.get_ident(), on_loop))

    monkeypatch.setattr(runner_module, "_reconcile_one_failed", fake_reconcile)
    monkeypatch.setattr(runner_module, "LAND_POLL_MIN_INTERVAL", 0.0)
    runner = _runner(client)

    async def scenario() -> int:
        await runner._reconcile_failed()
        return threading.get_ident()

    loop_thread = asyncio.run(scenario())
    runner._gitea_pool.shutdown(wait=True)
    assert seen and all(t != loop_thread and not on for t, on in seen)


def test_train_sweep_runs_off_the_loop(client, monkeypatch):
    seen: list[int] = []
    monkeypatch.setattr(train_module, "train_tick", lambda db: seen.append(threading.get_ident()))
    runner = _runner(client)

    async def scenario() -> int:
        await runner._tick(max_parallel=0)
        return threading.get_ident()

    loop_thread = asyncio.run(scenario())
    runner._gitea_pool.shutdown(wait=True)
    assert seen and all(t != loop_thread for t in seen)


def test_capped_sweep_returns_and_is_not_stacked(client, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    release = threading.Event()
    runner = _runner(client)

    async def scenario() -> float:
        started = time.monotonic()
        await runner._sweep_off_loop("land", lambda: release.wait(10), 0.2)
        await runner._sweep_off_loop("land", lambda: None, 0.2)  # skipped: in flight
        elapsed = time.monotonic() - started
        release.set()
        return elapsed

    elapsed = asyncio.run(scenario())
    runner._gitea_pool.shutdown(wait=True)
    assert elapsed < 2.0
    messages = [r.getMessage() for r in caplog.records]
    assert any("land sweep exceeded" in m for m in messages)
    assert any("land sweep still running" in m for m in messages)


def test_stop_shuts_the_gitea_pool_down(client):
    runner = _runner(client)
    asyncio.run(runner.stop())
    with pytest.raises(RuntimeError):
        runner._gitea_pool.submit(lambda: None)
