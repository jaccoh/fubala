"""Scheduler liveness: the loop survives every tick exception (the 26 Oct
02:52Z incident — 'database is locked' escaped _loop, killed the task and
froze the board for three hours) and the heartbeat is observable through
TWO probes with different jobs: /health stays 200 while the process serves
(its body carries the banner's stall flag on a short bound), /health/live
goes 503 only past max(600 s, 10x the poll interval) — a really dead loop,
never a legitimately long tick (a merge-train flush doing git fetch/merge/
push must not drop the pod off the ingress).

No time.sleep / sleep-polling anywhere: the story-status waits ride the
in-process event bus through the TestClient portal (frames are queued by
publish(), so a transition between subscribe and read can never be missed),
and the health checks assert on stamps written by start() or the loop itself.
"""

import asyncio
import json
import sqlite3
import sys
from datetime import timedelta

import pytest
from sqlalchemy.exc import OperationalError

from app.events import bus
from app.models import utcnow


def create_story(client, **body) -> dict:
    res = client.post("/api/v1/stories", json={"title": "liveness", **body})
    assert res.status_code == 201
    return res.json()


def _reached(status: str | None, want: str) -> bool:
    """`in_progress` means "the scheduler picked it up": a fast pipeline may
    already have moved the story on by the time the waiter looks."""
    if want == "in_progress":
        return status not in (None, "draft", "queued")
    return status == want


async def _drain_until_status(
    queue: asyncio.Queue, story_id: str, want: str, timeout: float
):
    """Consume bus frames until the story's frame with `want` status arrives.
    Returns the story payload, or None on the deadline (nothing published the
    transition — the scheduler truly never picked the story up)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            raw = await asyncio.wait_for(queue.get(), remaining)
            frame = json.loads(raw)
            if frame.get("kind") != "story":
                continue
            story = frame.get("data", {}).get("story", {})
            if story.get("id") == story_id and _reached(story.get("status"), want):
                return story
    finally:
        bus.unsubscribe(queue)


def wait_for_status(client, story_id: str, want: str, timeout: float = 30.0) -> dict:
    """Event-based status wait. Subscribes on the app's own event loop through
    the TestClient portal and blocks until the transition frame arrives — no
    polling loop, no sleeps."""
    queue = client.portal.call(bus.subscribe)
    # the transition may have fired before we subscribed (loaded runner)
    current = client.get(f"/api/v1/stories/{story_id}").json()
    if _reached(current.get("status"), want):
        client.portal.call(bus.unsubscribe, queue)
        return current
    payload = client.portal.call(_drain_until_status, queue, story_id, want, timeout)
    if payload is None:
        pytest.fail(f"story never reached {want}; the scheduler never picked it up")
    return payload


# ---- the loop must survive its own exceptions ---------------------------


def test_loop_survives_locked_database(client, monkeypatch):
    """Reproduction of the incident: load_settings raising
    OperationalError('database is locked') at the top of _loop used to escape
    the coroutine (it sat outside the try), kill the task start() created and
    freeze scheduling for the life of the process. The hardened loop backs
    off and retries, and the queued story still runs."""
    from app import runner as runner_mod

    real = runner_mod.load_settings
    fired = {"locked": False}

    def locked_once(engine):
        # raise ONLY for the loop's own top-of-loop call (the incident's
        # exact shape); pipeline and route callers pass through untouched
        if not fired["locked"] and sys._getframe(1).f_code.co_name == "_loop":
            fired["locked"] = True
            raise OperationalError(
                "select 1",
                None,
                sqlite3.OperationalError("database is locked"),
            )
        return real(engine)

    # call-time lookup: _loop reads the module global each iteration, so a
    # module-attr patch takes effect on the very next one
    monkeypatch.setattr(runner_mod, "load_settings", locked_once)

    story = create_story(client)
    assert client.post(f"/api/v1/stories/{story['id']}/queue").status_code == 200

    # only a live scheduler loop moves queued -> in_progress: this fails
    # against the unfixed _loop (the task is dead, no frame ever comes)
    wait_for_status(client, story["id"], "in_progress")
    assert fired["locked"], "the locked error never reached the loop top"

    runner = client.app.state.story_runner
    assert runner._task is not None and not runner._task.done(), (
        "the scheduler task died on the locked database"
    )
    # and the completed tick after the retry re-stamped the heartbeat
    res = client.get("/api/v1/health")
    assert res.status_code == 200
    assert res.json()["scheduler_stalled"] is False


def test_loop_survives_arbitrary_tick_error(client, monkeypatch):
    """Any non-lock exception inside a tick is logged and the loop continues —
    the catch-all the old loop had for _tick must survive the restructure."""
    from app import runner as runner_mod

    real = runner_mod.StoryRunner._tick
    boom = {"armed": True}

    async def exploding_tick(self, max_parallel):
        if boom["armed"]:
            boom["armed"] = False
            raise RuntimeError("boom: tick exploded")
        await real(self, max_parallel)

    monkeypatch.setattr(runner_mod.StoryRunner, "_tick", exploding_tick)

    story = create_story(client)
    assert client.post(f"/api/v1/stories/{story['id']}/queue").status_code == 200
    wait_for_status(client, story["id"], "in_progress")
    assert not boom["armed"], "the exploding tick never ran"

    runner = client.app.state.story_runner
    assert runner._task is not None and not runner._task.done()


# ---- the two probes: /health carries the banner, /health/live is the gate --


def _stopped_runner(client):
    """Scheduler HARD-off (pattern of test_headless.py:751): the loop task is
    cancelled and awaited, so nothing re-stamps the heartbeat afterwards and
    the tests can backdate it deterministically."""
    runner = client.app.state.story_runner
    client.portal.call(runner.stop)
    return runner


def test_health_stays_200_while_the_process_serves(client):
    """A dead scheduler (11 min without a completed tick) must NOT take
    /health down: the banner fields carry the stall, the status code stays
    200 — self-update, the menu bar and the readiness probe read non-200 as
    'the process is down', which it is not."""
    assert client.get("/api/v1/health").json()["scheduler_stalled"] is False

    runner = _stopped_runner(client)
    stalled_at = utcnow() - timedelta(minutes=11)
    runner._last_tick_at = stalled_at

    res = client.get("/api/v1/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["scheduler_stalled"] is True
    assert body["scheduler_last_tick"] == stalled_at.isoformat()

    # fresh stamp clears the banner flag
    runner._last_tick_at = utcnow()
    body = client.get("/api/v1/health").json()
    assert body["scheduler_stalled"] is False


def test_health_live_goes_503_only_on_a_dead_loop(client):
    """The dead-loop verdict: 200 while a live loop runs (fresh stamp), 200
    on a fresh stamp after stop() (the probe judges the heartbeat, not the
    task), and 503/unhealthy only once the last COMPLETED tick is far past
    the live bound."""
    assert client.get("/api/v1/health/live").status_code == 200

    runner = _stopped_runner(client)

    runner._last_tick_at = utcnow()
    res = client.get("/api/v1/health/live")
    assert res.status_code == 200
    assert res.json()["scheduler_stalled"] is False

    stalled_at = utcnow() - timedelta(minutes=11)
    runner._last_tick_at = stalled_at
    res = client.get("/api/v1/health/live")
    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "unhealthy"
    assert body["scheduler_stalled"] is True
    assert body["scheduler_last_tick"] == stalled_at.isoformat()


def test_health_live_tolerates_a_long_tick(client):
    """The reason for the split: a tick that legitimately runs long (3 min —
    a train flush doing git fetch/merge/push for several members) shows on
    the BANNER (short bound, the owner is told) but must not trip the LIVE
    probe (no NotReady, no mid-flush pod restart)."""
    runner = _stopped_runner(client)
    runner._tick_interval = 0.5  # fast poller: the live bound is the 600 s floor
    runner._last_tick_at = utcnow() - timedelta(minutes=3)

    banner = client.get("/api/v1/health").json()
    assert banner["scheduler_stalled"] is True  # past max(120, 3 x 0.5)

    live = client.get("/api/v1/health/live")
    assert live.status_code == 200
    assert live.json()["scheduler_stalled"] is False  # inside max(600, 5)


def test_health_live_bound_floors_and_scales(client):
    """Both terms of the live bound, with margins:
    - floor: max(600, ...) — 590 s stale answers 200, 620 s answers 503;
    - 10x the interval: a 90 s poller gets a 900 s bound, so 700 s stale
      (past the floor) still answers 200."""
    runner = _stopped_runner(client)

    runner._tick_interval = 0.5
    runner._last_tick_at = utcnow() - timedelta(seconds=590)
    assert client.get("/api/v1/health/live").status_code == 200
    runner._last_tick_at = utcnow() - timedelta(seconds=620)
    assert client.get("/api/v1/health/live").status_code == 503

    runner._tick_interval = 90.0  # 10 x 90 = 900 > 600
    runner._last_tick_at = utcnow() - timedelta(seconds=700)
    assert client.get("/api/v1/health/live").status_code == 200
    runner._last_tick_at = utcnow() - timedelta(seconds=950)
    assert client.get("/api/v1/health/live").status_code == 503


def test_health_banner_bound_floors_and_scales(client):
    """Same pair of terms for the banner bound (max(120, 3x interval)):
    60 s stale on a fast poller is healthy, 130 s flags; a 60 s poller gets
    a 180 s bound, so 150 s stale is still healthy."""
    runner = _stopped_runner(client)

    runner._tick_interval = 0.5
    runner._last_tick_at = utcnow() - timedelta(seconds=60)
    assert client.get("/api/v1/health").json()["scheduler_stalled"] is False
    runner._last_tick_at = utcnow() - timedelta(seconds=130)
    assert client.get("/api/v1/health").json()["scheduler_stalled"] is True

    runner._tick_interval = 60.0  # 3 x 60 = 180 > 120
    runner._last_tick_at = utcnow() - timedelta(seconds=150)
    assert client.get("/api/v1/health").json()["scheduler_stalled"] is False
    runner._last_tick_at = utcnow() - timedelta(seconds=200)
    assert client.get("/api/v1/health").json()["scheduler_stalled"] is True
