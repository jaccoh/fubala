"""The coder's run_verify tool + the vendored-dependency guard.

The coder used to guess at test commands (run_command with its own allowlist
and 180s cap) — or skip testing entirely. run_verify gives it the EXACT command
verify runs (same cwd, shared-cache env, timeout, warm-verify behavior), with
no parameters: the model cannot inject a command, it can only trigger the
operator's trusted one.

The vendored guard is the 16 Oct lesson: a coder that "fixes" tests by
committing node_modules / site-packages / .venv trees must never reach review
unflagged — first offense bounces back to the coder with the reason, a repeat
offense escalates to human review with the work preserved (never silently
discarded).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import pytest

from app import workspaces
from app.agent_tools import ToolBox
from app.runner import StoryRunner, vendored_guard


def init_repo(tmp_path: Path) -> str:
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
    return str(repo)


def make_worktree(repo: str, story_id: str) -> tuple[Path, str]:
    wt, base, _new = workspaces.create(repo, story_id)
    return Path(wt), base


# ---- 1. tool surface ------------------------------------------------------


def test_run_verify_not_listed_unconfigured(tmp_path):
    """No verify config -> no run_verify in the schema (the model must never
    see a tool that can only answer 'not configured')."""
    repo = init_repo(tmp_path)
    tb = ToolBox(project_root=repo, workspace=repo, writable=True)
    assert "run_verify" not in [t["function"]["name"] for t in tb.schemas()]


def test_run_verify_listed_when_configured(tmp_path):
    repo = init_repo(tmp_path)
    tb = ToolBox(
        project_root=repo,
        workspace=repo,
        writable=True,
        verify_command="pytest -q",
        verify_timeout=90.0,
    )
    names = [t["function"]["name"] for t in tb.schemas()]
    assert "run_verify" in names
    schema = next(t for t in tb.schemas() if t["function"]["name"] == "run_verify")
    props = schema["function"]["parameters"].get("properties") or {}
    assert props == {}, "run_verify must accept NO parameters (trusted command only)"


def test_run_verify_runs_project_command_in_worktree(tmp_path):
    """echo ok && exit 3 -> exit_code 3 with 'ok' in the output tail."""
    repo = init_repo(tmp_path)
    wt, _base = make_worktree(repo, "s1")
    tb = ToolBox(
        project_root=repo,
        workspace=str(wt),
        writable=True,
        verify_command="echo ok && exit 3",
        verify_timeout=60.0,
    )
    result = tb.execute("run_verify", "{}")
    payload = json.loads(result)
    assert payload["exit_code"] == 3
    assert "ok" in payload["output"]
    # no arguments accepted / none used — the command is the operator's
    assert payload.get("command") == "echo ok && exit 3"


def test_run_verify_cwd_is_the_worktree(tmp_path):
    """The command runs in the worktree, not the main checkout: pwd must land
    under .fubala-worktrees/<id>."""
    repo = init_repo(tmp_path)
    wt, _base = make_worktree(repo, "s-cwd")
    tb = ToolBox(
        project_root=repo,
        workspace=str(wt),
        writable=True,
        verify_command="pwd",
        verify_timeout=30.0,
    )
    payload = json.loads(tb.execute("run_verify", "{}"))
    assert payload["exit_code"] == 0
    assert str(wt.resolve()) in payload["output"]
    assert ".fubala-worktrees" in payload["output"]


def test_run_verify_reuses_run_verify_command_semantics(tmp_path):
    """Timeout and OSError sentinels pass through: a hanging command yields the
    same rc 124 the verify stage reports, as a FAILURE for the coder."""
    repo = init_repo(tmp_path)
    wt, _base = make_worktree(repo, "s-timeout")
    tb = ToolBox(
        project_root=repo,
        workspace=str(wt),
        writable=True,
        verify_command="sleep 5",
        verify_timeout=1.0,
    )
    payload = json.loads(tb.execute("run_verify", "{}"))
    assert payload["exit_code"] == 124
    assert payload["passed"] is False
    assert "timed out" in payload["output"]


def test_run_verify_output_capped_at_4000(tmp_path):
    repo = init_repo(tmp_path)
    wt, _base = make_worktree(repo, "s-cap")
    tb = ToolBox(
        project_root=repo,
        workspace=str(wt),
        writable=True,
        verify_command="python3 -c \"print('x' * 20000)\"",
        verify_timeout=30.0,
    )
    payload = json.loads(tb.execute("run_verify", "{}"))
    assert len(payload["output"]) <= 4000


def test_run_verify_without_config_errors(tmp_path):
    repo = init_repo(tmp_path)
    wt, _base = make_worktree(repo, "s-nocfg")
    tb = ToolBox(project_root=repo, workspace=str(wt), writable=True)
    assert "error" in json.loads(tb.execute("run_verify", "{}"))


def test_run_verify_ignores_injected_arguments(tmp_path):
    """Even if a model passes arguments (it was told none exist), the trusted
    command runs unchanged — arguments cannot become shell."""
    repo = init_repo(tmp_path)
    wt, _base = make_worktree(repo, "s-inject")
    tb = ToolBox(
        project_root=repo,
        workspace=str(wt),
        writable=True,
        verify_command="echo trusted",
        verify_timeout=30.0,
    )
    payload = json.loads(
        tb.execute(
            "run_verify",
            json.dumps({"command": "echo pwned", "timeout": 1}),
        )
    )
    assert payload["command"] == "echo trusted"
    assert "pwned" not in payload["output"]


# ---- 2. off-loop execution ------------------------------------------------


def test_run_verify_stays_off_the_event_loop(tmp_path):
    """A slow run_verify call must not block the loop (same pin as the verify
    stage in test_event_loop_responsiveness.py): while it runs, the loop still
    answers — and the resolution helper stays sync so the call site can thread
    it."""
    from app.main import create_app

    repo = init_repo(tmp_path)
    wt, _base = make_worktree(repo, "s-loop")

    class GW:
        async def stream_chat(self, *, model, messages, tools=None):
            from app.providers import Delta as D
            from app.providers import Usage as U

            yield D(text="ok")
            yield U(prompt_tokens=1, completion_tokens=1)

    import httpx
    from fastapi.testclient import TestClient

    monkeydb = tmp_path / "loop.db"
    import os

    os.environ["FUBALA_DATABASE_PATH"] = str(monkeydb)
    try:
        client = TestClient(create_app(gateway=GW()))
        with client:
            client.patch(
                "/api/v1/settings",
                json={"story_poll_interval": 60, "project_root": repo},
            )
            tb = ToolBox(
                project_root=repo,
                workspace=str(wt),
                writable=True,
                verify_command="sleep 2",
                verify_timeout=30.0,
                engine=client.app.state.engine,
            )
            toolbox = tb

            async def scenario() -> None:
                started = time.monotonic()
                call = asyncio.create_task(
                    asyncio.to_thread(toolbox.execute, "run_verify", "{}")
                )
                # mirror _probe_health: the wall clock is the honest probe
                probe_started = time.monotonic()
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=client.app),
                    base_url="http://testserver",
                    # not a wait: the probe must itself be fast to prove the
                    # loop stayed responsive behind the running verify tool
                    timeout=0.5,
                ) as api:
                    res = await api.get("/api/v1/health")
                elapsed = time.monotonic() - probe_started
                assert res.status_code == 200, res.text
                assert elapsed < 0.5, (
                    f"/health blocked {elapsed:.2f}s behind a running verify tool"
                )
                payload = json.loads(await call)
                assert payload["exit_code"] == 0
                assert time.monotonic() - started >= 1.5  # it really ran

            asyncio.run(scenario())
    finally:
        os.environ.pop("FUBALA_DATABASE_PATH", None)


def test_resolve_verify_command_helper(tmp_path):
    """The extracted resolution: global setting, project override wins."""
    from sqlmodel import Session

    from app.config import Settings
    from app.db import init_db, make_engine
    from app.models import Project
    from app.runner import resolve_verify_command
    from app.settings_store import save_settings

    engine = make_engine(Settings(database_path=tmp_path / "resolve.db"))
    init_db(engine)
    save_settings(engine, {"verify_command": "global-cmd", "verify_timeout": 123})
    repo = init_repo(tmp_path)

    # no project -> global
    with Session(engine) as db:
        command, timeout = resolve_verify_command(db, None)
    assert command == "global-cmd" and timeout == 123.0

    # project with its own override -> override, global timeout
    with Session(engine) as db:
        proj = Project(name="demo", root=repo, verify_command="  make test  ")
        db.add(proj)
        db.commit()
        db.refresh(proj)
        pid = proj.id
    with Session(engine) as db:
        command, timeout = resolve_verify_command(db, pid)
    assert command == "make test"
    assert timeout == 123.0


# ---- 3. prompt ------------------------------------------------------------


def test_coder_prompt_names_run_verify_and_bans_vendoring():
    from app.runner import CODER_PROMPT

    prompt = CODER_PROMPT.read_text()
    assert "run_verify" in prompt
    low = prompt.lower()
    assert "never install" in low and "vendor" in low


# ---- 4. vendored guard (pure function, fake diff inputs) -------------------


def test_guard_clean_diff_is_none():
    assert vendored_guard(["src/app.py", "tests/test_x.py"]) is None


@pytest.mark.parametrize(
    "path",
    [
        "node_modules/react/index.js",
        "site-packages/numpy/__init__.py",
        ".venv/bin/python",
        ".local-lib/foo.py",
        "frontend/node_modules/.bin/tsc",
        "backend/.venv/pyvenv.cfg",
    ],
)
def test_guard_flags_vendored_segments(path):
    reason = vendored_guard([path])
    assert reason is not None
    assert path in reason, "the reason must sample the offending paths"


def test_guard_flags_mass_file_additions():
    paths = [f"gen/f{i}.txt" for i in range(201)]
    reason = vendored_guard(paths)
    assert reason is not None
    assert "200" in reason
    # exactly at the limit is fine
    assert vendored_guard(paths[:200]) is None


def test_guard_reason_includes_samples():
    paths = [f"node_modules/pkg{i}/index.js" for i in range(50)]
    reason = vendored_guard(paths)
    assert "node_modules/pkg0/index.js" in reason


# ---- 5. wired into the runner ---------------------------------------------


class ScriptedGW:
    """Plays a fixed sequence of stream events, one list per gateway call.

    Call order for a code story: spec (1), spec ai-review (2), coder rounds
    (2 gateway calls each: tool round + closing text round), then one code
    review call — unless the guard requeues/escalates first, which changes
    the count (that is the point: the script only matches when the pipeline
    follows the expected path)."""

    def __init__(self, moves: list[list]):
        self.moves = moves
        self.calls = 0

    async def stream_chat(self, *, model, messages, tools=None, **kwargs):
        from app.providers import Delta as D
        from app.providers import Usage as U

        i = self.calls
        self.calls += 1
        for event in (
            self.moves[i] if i < len(self.moves) else [D("VERDICT: APPROVED")]
        ):
            yield event
        yield U(prompt_tokens=1, completion_tokens=1)


def _tc_write(path: str, content: str, tid: str):
    from app.providers import ToolCall as TC

    return TC(
        id=tid,
        name="write_file",
        arguments=json.dumps({"path": path, "content": content}),
    )


def _tc_run(command: str, tid: str):
    from app.providers import ToolCall as TC

    return TC(id=tid, name="run_command", arguments=json.dumps({"command": command}))


# passes the runner's REQUIRED_SPEC_SECTIONS deliverable guard (same shape as
# conftest.MINIMAL_SPEC — this file is standalone, so it carries its own copy)
SPEC_OK = (
    "## Goal\nok\n\n"
    "## Implementation steps\n1. do it\n\n"
    "## Acceptance checks\n- [ ] tests pass\n"
)


def _wait_status(client, sid, want, timeout=30.0):
    deadline = time.time() + timeout
    cur = None
    while time.time() < deadline:
        cur = client.get(f"/api/v1/stories/{sid}").json()
        if cur["status"] in want:
            return cur
        time.sleep(0.05)
    pytest.fail(f"never reached {want}; last: {cur and cur['status']}")


def _guard_notes(engine) -> list[str]:
    """All vendored-guard note reasons, oldest first (DB = source of truth)."""
    from sqlmodel import Session, select

    from app.models import StoryEvent

    with Session(engine) as db:
        rows = db.exec(
            select(StoryEvent)
            .where(StoryEvent.kind == "note")
            .order_by(StoryEvent.id)
        ).all()
        out = []
        for ev in rows:
            data = json.loads(ev.data or "{}")
            if isinstance(data, dict) and data.get("by") == "vendored-guard":
                out.append(data.get("reason") or "")
    return out


def _wait_guard_note(engine, count: int, timeout: float = 30.0) -> list[str]:
    deadline = time.time() + timeout
    notes: list[str] = []
    while time.time() < deadline:
        notes = _guard_notes(engine)
        if len(notes) >= count:
            return notes
        time.sleep(0.05)
    pytest.fail(f"expected {count} vendored-guard note(s), got {notes}")


def _run_guard_story(tmp_path, monkeypatch, gateway) -> tuple[object, str]:
    """Queue a code story through spec + ai review + human approve; returns
    (TestClient, story_id) with the coder pipeline about to start."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "guard.db"))
    client = TestClient(create_app(gateway=gateway))
    client.__enter__()
    client.patch(
        "/api/v1/settings",
        json={
            "story_poll_interval": 0.05,
            "project_root": repo,
            "write_tools_enabled": True,
        },
    )
    sid = client.post("/api/v1/stories", json={"title": "guard me"}).json()["id"]
    client.post(f"/api/v1/stories/{sid}/queue")
    # spec -> ai review -> human gate -> approved -> coder runs
    _wait_status(client, sid, ("human_review", "failed"))
    client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
    return client, sid


def test_guard_first_offense_requeues_clean_retry_proceeds(tmp_path, monkeypatch):
    """Dump .local-lib/ -> guard note + requeue to the coder; a retry that
    purges the vendored dir and writes real code flows on to human review
    with a clean diff (retries accumulate vs base_ref, not HEAD)."""
    from app.providers import Delta as D

    moves = [
        [D(SPEC_OK)],  # spec writer
        [D("VERDICT: APPROVED")],  # spec ai review
        # coder attempt 1: vendored dump (first offense)
        [_tc_write(".local-lib/marker.txt", "x\n", "w1"), D("done")],
        [D("done")],
        # (guard requeues; code review is skipped for a QUEUED story)
        # coder attempt 2: purge the committed vendored dir, write clean code
        [
            _tc_run("git rm -rq .local-lib", "c1"),
            _tc_write("feature.py", "VALUE = 'ok'\n", "w2"),
            D("done"),
        ],
        [D("done")],
        [D("VERDICT: APPROVED")],  # code review
    ]
    client, sid = _run_guard_story(tmp_path, monkeypatch, ScriptedGW(moves))
    try:
        # first offense: a vendored-guard note samples the offending path...
        notes = _wait_guard_note(client.app.state.engine, 1)
        assert ".local-lib/marker.txt" in notes[0]
        # ...and the story went back to the coder (the note becomes
        # prev_feedback on the retry)
        _wait_status(client, sid, ("queued", "executing", "human_review", "failed"))
        # clean retry reaches human review through the normal code review
        final = _wait_status(client, sid, ("human_review", "failed"))
        assert final["status"] == "human_review", final.get("error")
        diff = final.get("code_diff") or ""
        assert "feature.py" in diff
        assert ".local-lib" not in diff
        assert len(notes) == 1  # no second offense
    finally:
        client.__exit__(None, None, None)


def test_guard_repeat_offense_escalates_to_human_review(tmp_path, monkeypatch):
    """Second vendored offense -> HUMAN_REVIEW with the work preserved and a
    visible marker (verify_result names the guard) — never silently deleted."""
    from app.providers import Delta as D

    moves = [
        [D(SPEC_OK)],  # spec writer
        [D("VERDICT: APPROVED")],  # spec ai review
        # coder attempt 1: vendored dump (first offense -> requeue)
        [_tc_write(".local-lib/marker.txt", "x\n", "w1"), D("done")],
        [D("done")],
        # coder attempt 2: dumps AGAIN (repeat offense -> escalate)
        [_tc_write("node_modules/pkg/index.js", "x\n", "w3"), D("done")],
        [D("done")],
    ]
    client, sid = _run_guard_story(tmp_path, monkeypatch, ScriptedGW(moves))
    try:
        notes = _wait_guard_note(client.app.state.engine, 2)
        assert ".local-lib/marker.txt" in notes[0]
        assert "node_modules/pkg/index.js" in notes[1]
        final = _wait_status(client, sid, ("human_review", "failed"))
        assert final["status"] == "human_review", final.get("error")
        # visible marker on the story: the verify panel names the guard
        vr = final.get("verify_result") or {}
        assert vr.get("command") == "vendored-guard"
        assert vr.get("passed") is False
        assert ".local-lib/marker.txt" in (vr.get("output") or "")
        # work preserved — both offenses still in the diff for the human
        diff = final.get("code_diff") or ""
        assert ".local-lib/marker.txt" in diff
        assert "node_modules/pkg/index.js" in diff
    finally:
        client.__exit__(None, None, None)


def test_run_verify_through_guarded_executor_keeps_the_loop_responsive(tmp_path):
    """Gate point 1 (coordinator): the REAL call path. The agent awaits
    GuardedExecutor.__call__ on the server's event loop; a run_verify of
    minutes must not freeze that loop (/health, SSE, other stories) -- the
    bug 5eb2031d fixed for the verify stage. No asyncio.to_thread in the test
    itself: the executor has to provide it."""
    import asyncio
    import time

    from app.agent_tools import ToolBox
    from app.guardrails import GuardedExecutor, ToolPolicy

    repo = init_repo(tmp_path)
    toolbox = ToolBox(
        project_root=repo,
        workspace=repo,
        writable=True,
        verify_command="sleep 2",
        verify_timeout=30.0,
    )
    guarded = GuardedExecutor(toolbox, ToolPolicy(max_tool_calls=5, timeout_seconds=60.0))

    async def scenario():
        task = asyncio.create_task(guarded("run_verify", "{}"))
        # measure from BEFORE the first yield: the task starts on that yield,
        # and a synchronous tool would hold the loop until it returns
        t0 = time.monotonic()
        await asyncio.sleep(0.1)
        lag = time.monotonic() - t0
        result = await task
        return lag, result

    lag, result = asyncio.run(scenario())
    assert lag < 0.5, f"event loop blocked {lag:.2f}s behind a running run_verify"
    assert '"exit_code": 0' in result
