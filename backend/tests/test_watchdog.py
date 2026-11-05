"""Watchdog phase floors (the 27 Oct incident).

A run inside a phase with its own wall-clock bound — the runner's verify
rounds, tool-invoked verifies (run_tests/run_verify), the headless CODE
stage, guarded tool calls — is bus-silent BY DESIGN. The stream watchdog
must not cancel it before that phase's own timeout (+ margin) resolves it;
a genuinely silent stream in every unbounded phase (gateway streaming,
agent rounds) must still be cancelled and requeued.

Scaled constants keep this out of the known-flaky class: the margins
between watchdog, verify and verify_timeout are wide, every wait is a
deadline poll, and no test races the scheduler with a sleep.
"""

import asyncio
import json
import sqlite3
import subprocess
import time
from contextlib import contextmanager

import pytest
from conftest import MINIMAL_SPEC
from fastapi.testclient import TestClient

from app import runner as runner_module
from app.main import create_app


def init_repo(tmp_path) -> str:
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return str(repo)


class CoderGW:
    """Routes every runner phase on its prompt marker (never a call counter),
    so a requeued story cannot exhaust the script.

    spec -> valid spec, spec review -> APPROVED, coder -> writes feature.py,
    fix round -> plain text, code review -> APPROVED."""

    @staticmethod
    def _last_user_text(messages) -> str:
        for message in reversed(messages):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
        from app.providers import Delta as D
        from app.providers import ToolCall as TC
        from app.providers import Usage as U

        prompt = self._last_user_text(messages)
        after_tool_round = any(m.get("role") == "tool" for m in messages)
        if "=== SPEC REQUEST ===" in prompt:
            yield D(text=MINIMAL_SPEC)
        elif "=== AI REVIEW REQUEST ===" in prompt:
            yield D(text="fine\n\nVERDICT: APPROVED")
        elif "=== CODE REQUEST ===" in prompt and not after_tool_round:
            yield TC(
                id="w1",
                name="write_file",
                arguments=json.dumps(
                    {"path": "feature.py", "content": "VALUE = 'watchdog'\n"}
                ),
            )
        elif "=== CODE REQUEST ===" in prompt or "=== FIX REQUEST ===" in prompt:
            yield D(text="implemented feature.py")
        else:
            yield D(text="diff matches spec\n\nVERDICT: APPROVED")
        yield U(prompt_tokens=1, completion_tokens=1)


@contextmanager
def watchdog_client(
    tmp_path,
    monkeypatch,
    db_name,
    *,
    watchdog_timeout,
    gateway,
    settings=None,
    raw_settings=None,
):
    """A TestClient against a fresh DB with a real repo, write tools on and a
    scaled WATCHDOG_TIMEOUT — everything the pipeline needs to reach verify.

    `raw_settings` is written through save_settings, bypassing the API schema
    (verify_timeout is ge=10 there, and the verify-timeout path needs 1)."""
    repo = init_repo(tmp_path)
    db_path = tmp_path / db_name
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
    monkeypatch.setattr(runner_module, "WATCHDOG_TIMEOUT", watchdog_timeout)
    with TestClient(create_app(gateway=gateway)) as client:
        body = {
            "story_poll_interval": 0.05,
            "project_root": repo,
            "write_tools_enabled": True,
        }
        body.update(settings or {})
        res = client.patch("/api/v1/settings", json=body)
        assert res.status_code == 200, res.text
        if raw_settings:
            from app.settings_store import save_settings

            save_settings(client.app.state.engine, raw_settings)
        yield client, db_path


def wait_status(client, sid, want, timeout=60.0) -> dict:
    deadline = time.time() + timeout
    cur = None
    while time.time() < deadline:
        cur = client.get(f"/api/v1/stories/{sid}").json()
        if cur["status"] in want:
            return cur
        time.sleep(0.05)
    pytest.fail(
        f"never reached {want}; last: {cur and cur['status']} {cur and cur.get('error')}"
    )


def wait_coder_done(client, sid, timeout=60.0) -> dict:
    """Wait until the coder stage produced a verify_result AND the pipeline
    parked somewhere terminal-ish (human_review/done/failed)."""
    deadline = time.time() + timeout
    cur = None
    while time.time() < deadline:
        cur = client.get(f"/api/v1/stories/{sid}").json()
        if cur.get("verify_result") and cur["status"] in (
            "human_review",
            "done",
            "failed",
        ):
            return cur
        time.sleep(0.05)
    pytest.fail(
        f"coder stage never finished; last: "
        f"{cur and cur['status']} {cur and cur.get('error')}"
    )


def watchdog_note(db_path) -> str | None:
    """The first watchdog requeue status event, if any."""
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute(
            "SELECT data FROM story_events "
            "WHERE kind='status' AND data LIKE '%watchdog%'"
        ).fetchall()
    finally:
        db.close()
    return rows[0][0] if rows else None


def run_to_coder(client) -> str:
    """A story through the spec gate (human): park at human_review, approve,
    so the coder stage (worktree -> verify -> code review) starts."""
    story = client.post("/api/v1/stories", json={"title": "w"}).json()
    sid = story["id"]
    client.post(f"/api/v1/stories/{sid}/queue")
    parked = wait_status(client, sid, ("human_review", "failed"))
    assert parked["status"] == "human_review", parked.get("error")
    client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
    return sid


# ---- AC1: a healthy verify longer than WATCHDOG_TIMEOUT survives ----------


def test_verify_outlasting_watchdog_survives(tmp_path, monkeypatch):
    """AC1 (regression, 27 Oct): a verify that outlasts WATCHDOG_TIMEOUT but
    not verify_timeout is healthy — it must pass and carry the story past
    executing, with no watchdog requeue. Red on unfloored main, where the
    watchdog killed prod verifies at ~900s."""
    with watchdog_client(
        tmp_path,
        monkeypatch,
        "wd-ac1.db",
        watchdog_timeout=2.0,
        gateway=CoderGW(),
        settings={"verify_command": "sleep 5", "verify_timeout": 30},
    ) as (client, db_path):
        sid = run_to_coder(client)
        final = wait_coder_done(client, sid)
        assert final["status"] != "failed", final.get("error")
        verify = final["verify_result"]
        assert verify["passed"] is True
        assert verify["exit_code"] == 0
        assert watchdog_note(db_path) is None


# ---- AC2: silence outside any bounded phase is still fatal -----------------


def test_silent_stream_outside_bounded_phase_still_requeued(tmp_path, monkeypatch):
    """AC2: the floor must never immunize an unbounded phase — a gateway that
    streams one fragment then blocks forever is cancelled and requeued after
    WATCHDOG_TIMEOUT, note and all."""

    class OneThenSilentGW:
        def __init__(self):
            self.calls = 0

        async def stream_chat(
            self, *, model, messages, tools=None, max_output_tokens=None
        ):
            from app.providers import Delta as D
            from app.providers import Usage as U

            self.calls += 1
            yield D(text=MINIMAL_SPEC)
            await asyncio.sleep(3600)  # alive but mute, forever
            yield U(prompt_tokens=1, completion_tokens=1)  # pragma: no cover

    with watchdog_client(
        tmp_path, monkeypatch, "wd-ac2.db", watchdog_timeout=0.5, gateway=OneThenSilentGW()
    ) as (client, db_path):
        story = client.post("/api/v1/stories", json={"title": "hangs"}).json()
        sid = story["id"]
        client.post(f"/api/v1/stories/{sid}/queue")

        note = None
        deadline = time.time() + 30
        while time.time() < deadline:
            note = watchdog_note(db_path)
            if note:
                break
            time.sleep(0.1)
        assert note, "no watchdog requeue event recorded"
        assert "watchdog: stream hung, requeued" in note
        current = client.get(f"/api/v1/stories/{sid}").json()
        assert current["status"] != "failed", current.get("error")


# ---- AC3: verify's own timeout path stays the outcome ----------------------


def test_verify_own_timeout_drives_outcome_not_watchdog(tmp_path, monkeypatch):
    """AC3: a verify past verify_timeout fails through the existing
    verify-timeout path (rc 124 + the timeout message) — the watchdog never
    intervenes, and the failing result still gates the story."""
    with watchdog_client(
        tmp_path,
        monkeypatch,
        "wd-ac3.db",
        watchdog_timeout=2.0,
        gateway=CoderGW(),
        settings={"verify_command": "sleep 5"},
        # verify_timeout must be 1 (the API schema floors it at 10), so it
        # rides the raw store
        raw_settings={"verify_timeout": 1.0},
    ) as (client, db_path):
        sid = run_to_coder(client)
        final = wait_coder_done(client, sid)
        assert final["status"] != "failed", final.get("error")
        verify = final["verify_result"]
        assert verify["passed"] is False
        assert verify["exit_code"] == 124
        assert "verify timed out after" in verify["output"]
        assert watchdog_note(db_path) is None


# ---- unit: the floor semantics themselves ----------------------------------


def test_watchdog_floor_semantics(tmp_path, monkeypatch, gateway):
    """The sweep skips a silent run whose floor is in the future and fires
    once the floor is past; _set_watchdog_floor stamps now + bound + margin,
    and the fire path pops the floor (no leaked immunity)."""
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "wd-floor.db"))
    monkeypatch.setattr(runner_module, "WATCHDOG_TIMEOUT", 0.2)
    monkeypatch.setattr(runner_module, "WATCHDOG_PHASE_MARGIN", 5.0)
    with TestClient(create_app(gateway=gateway)) as client:
        runner = client.app.state.story_runner
        # scheduler hard-off: no tick may sweep or requeue while we drive the
        # runner's state directly
        client.portal.call(runner.stop)

        sid = "floor-unit"
        runner._running.add(sid)
        runner._last_event[sid] = time.monotonic()

        # stamp: floor = now + bound + margin
        before = time.monotonic()
        runner._set_watchdog_floor(sid, 10.0)
        floor = runner._watchdog_floor[sid]
        assert before + 15.0 <= floor <= time.monotonic() + 15.0

        # silence far past WATCHDOG_TIMEOUT, floor still in the future: the
        # sweep must leave the run alone
        runner._last_event[sid] = time.monotonic() - 5.0
        client.portal.call(runner._watchdog_sweep)
        assert sid in runner._running
        assert runner._last_event.get(sid) is not None

        # floor expired: the same silence now cancels the run, and the fire
        # path leaves no floor entry behind
        runner._watchdog_floor[sid] = time.monotonic() - 0.001
        client.portal.call(runner._watchdog_sweep)
        assert sid not in runner._running
        assert runner._last_event.get(sid) is None
        assert sid not in runner._watchdog_floor

        # clear is a no-op on an absent key (every stamp site's finally)
        runner._clear_watchdog_floor(sid)
