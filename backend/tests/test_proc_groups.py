"""Every Fubala-spawned subprocess dies with the thing that stopped waiting.

Incident: six `sh tools/test.sh` -> `pytest -n 4` trees survived a story
cancel/timeout and burned ~4.5 cores for hours. `subprocess.run(...,
timeout=)` kills the direct child only — the grandchildren (xdist workers,
npm helpers) keep running because nobody signals their process group.

These tests pin the fix:
1. `_run_verify_command` timeouts kill the WHOLE group (shell + backgrounded
   grandchild), not just the `/bin/sh` it spawned,
2. `StoryRunner.cancel` leaves nothing behind for the story,
3. `procs.sweep` kills unregistered marked groups and spares those belonging
   to a running story,
4. the startup sweep actually runs,
5. `ToolBox._run_command` gets the same group treatment.

/proc-dependent tests skip where /proc does not exist (macOS dev machines);
Linux CI runs everything.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from app import procs
from app.runner import StoryRunner, _run_verify_command

ON_LINUX = procs._HAS_PROC

pytestmark = pytest.mark.skipif(not ON_LINUX, reason="requires /proc")


# ---- helpers ---------------------------------------------------------------


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("# demo\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return repo


def _alive_pids_in_group(pgid: int) -> set[int]:
    """Live (non-zombie) pids still belonging to `pgid`, via /proc."""
    alive: set[int] = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            raw = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        rest = raw[raw.rfind(")") + 1 :].split()
        if len(rest) < 3 or rest[0] == "Z":
            continue
        try:
            if int(rest[2]) == pgid:
                alive.add(int(entry))
        except ValueError:
            continue
    return alive


def _pid_alive(pid: int) -> bool:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    rest = raw[raw.rfind(")") + 1 :].split()
    return bool(rest) and rest[0] != "Z"


def _wait_gone(predicate, timeout: float = 15.0) -> None:
    """Poll until `predicate()` is falsy (bounded by the SIGKILL grace)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not predicate():
            return
        time.sleep(0.1)
    raise AssertionError(
        f"processes still alive after {timeout}s: {predicate()!r}"
    )


# the shell writes its own pid and its backgrounded child's pid, then execs a
# long sleep: after the timeout BOTH must be dead
SPAWNING_COMMAND = (
    "echo $$ >{pidfile}; sleep 300 & echo $! >>{pidfile}; exec sleep 300"
)


# ---- 1. verify timeout kills the whole tree --------------------------------


def test_verify_timeout_kills_grandchildren(tmp_path):
    repo = init_repo(tmp_path)
    ws = repo / ".fubala-worktrees" / "s"
    ws.mkdir(parents=True)
    pidfile = ws / "pids"

    rc, output = _run_verify_command(
        ws, SPAWNING_COMMAND.format(pidfile=pidfile), 1.0, str(repo)
    )

    assert rc == 124
    assert "verify timed out after 1.0s" in output
    shell_pid, child_pid = (int(p) for p in pidfile.read_text().split())
    assert not _pid_alive(shell_pid)
    assert not _pid_alive(child_pid), "the backgrounded grandchild survived"


def test_verify_timeout_leaves_no_group_member(tmp_path):
    """No process at all remains in the spawned group — not just the two pids
    we happen to know about."""
    repo = init_repo(tmp_path)
    ws = repo / ".fubala-worktrees" / "s2"
    ws.mkdir(parents=True)

    pgidfile = ws / "pgid"
    rc, _ = _run_verify_command(
        ws,
        "echo $$ >" + str(pgidfile) + "; sleep 300 & exec sleep 300",
        1.0,
        str(repo),
    )
    assert rc == 124
    pgid = int(pgidfile.read_text().strip())
    _wait_gone(lambda: _alive_pids_in_group(pgid))


def test_verify_timeout_returns_promptly(tmp_path):
    """The SIGTERM-first kill does not make the caller wait the full grace:
    processes that honour SIGTERM (sleep) die immediately."""
    repo = init_repo(tmp_path)
    ws = repo / ".fubala-worktrees" / "s3"
    ws.mkdir(parents=True)
    start = time.monotonic()
    rc, _ = _run_verify_command(ws, "sleep 300", 0.5, str(repo))
    elapsed = time.monotonic() - start
    assert rc == 124
    assert elapsed < 5, f"the timeout path took {elapsed:.1f}s"


# ---- 2. StoryRunner.cancel kills the story's tree --------------------------


def _make_runner(tmp_path: Path) -> StoryRunner:
    from sqlalchemy import Engine

    from app.config import Settings
    from app.db import init_db, make_engine

    db_path = tmp_path / "procs.db"
    engine: Engine = make_engine(Settings(database_path=str(db_path)))
    init_db(engine)
    from app.providers import Delta
    from app.registry import ProviderRegistry

    class _StubGateway:
        """The protocol's smallest stand-in: no call ever reaches it here."""

        async def stream(self, *a, **k):  # pragma: no cover - never called
            yield Delta(text="")

    registry = ProviderRegistry(engine)
    return StoryRunner(engine, _StubGateway(), registry=registry)  # type: ignore[arg-type]


def test_cancel_kills_registered_group(tmp_path):
    runner = _make_runner(tmp_path)
    repo = init_repo(tmp_path / "repo2")
    ws = repo / ".fubala-worktrees" / "s-cancel"
    ws.mkdir(parents=True)
    pidfile = ws / "pids"
    story_id = "story-cancel-me"

    async def scenario() -> tuple[int, int, int]:
        async def verify() -> None:
            # a verify running "inside" a story: same registration the
            # runner's verify stage performs
            await asyncio.to_thread(
                _run_verify_command,
                ws,
                SPAWNING_COMMAND.format(pidfile=pidfile),
                300.0,
                str(repo),
                story_id,
            )

        task = asyncio.ensure_future(verify())
        # the scheduler's bookkeeping: a story "in flight" owns its task
        runner._running.add(story_id)
        runner._tasks[story_id] = task
        # wait until the verify child is actually spawned and registered
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not procs.registered_groups(story_id):
            await asyncio.sleep(0.05)
        groups = procs.registered_groups(story_id)
        assert groups, "the verify group was never registered"
        assert await runner.cancel(story_id)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        shell_pid, child_pid = (int(p) for p in pidfile.read_text().split())
        return next(iter(groups)), shell_pid, child_pid

    pgid, shell_pid, child_pid = asyncio.run(scenario())
    _wait_gone(lambda: _alive_pids_in_group(pgid))
    assert not _pid_alive(shell_pid)
    assert not _pid_alive(child_pid)
    assert not procs.registered_groups(story_id)


# ---- 3. the sweep ----------------------------------------------------------


def test_sweep_kills_unregistered_and_spares_running(tmp_path, caplog):
    marked = procs.mark_env(dict(os.environ), "sweep-story")
    assert marked[procs.MARKER] == "1"
    assert marked[procs.STORY_ID_VAR] == "sweep-story"

    # a spawner that is already gone: what a SIGKILLed server leaves behind
    gone = subprocess.Popen(["true"])
    gone.wait(timeout=5)
    orphan_env = dict(marked)
    orphan_env[procs.SPAWNER_PID_VAR] = str(gone.pid)

    victim = subprocess.Popen(
        ["sleep", "300"],
        env=orphan_env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    kept = subprocess.Popen(
        ["sleep", "300"],
        env=procs.mark_env(dict(os.environ), "running-story"),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        victim_pgid = os.getpgid(victim.pid)
        kept_pgid = os.getpgid(kept.pid)
        # the "running" story's group is registered; the victim's is not
        procs.register("running-story", kept_pgid)
        with caplog.at_level(logging.WARNING, logger="app.procs"):
            killed = procs.sweep({"running-story"})
        assert victim_pgid in killed
        assert kept_pgid not in killed
        assert any(
            "process group" in r.message and str(victim_pgid) in r.message
            for r in caplog.records
        )
        _wait_gone(lambda: _alive_pids_in_group(victim_pgid))
        assert _alive_pids_in_group(kept_pgid), "a running story's group was killed"
    finally:
        procs.kill_group(kept_pgid, wait=False, proc=kept)
        procs.kill_group(victim_pgid, wait=False, proc=victim)


def test_sweep_spares_another_live_instance(tmp_path):
    """A marked group whose SPAWNER is still alive is not an orphan: another
    Fubala (or another xdist worker running this suite) owns it. Only a dead
    spawner's groups are swept."""
    # a separate live process is the "other live instance" (this process's own
    # groups are spared only while active)
    owner = subprocess.Popen(["sleep", "300"])
    other_env = dict(os.environ)
    other_env[procs.MARKER] = "1"
    other_env[procs.STORY_ID_VAR] = "other-boards-story"
    other_env[procs.SPAWNER_PID_VAR] = str(owner.pid)
    child = subprocess.Popen(
        ["sleep", "300"],
        env=other_env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pgid = os.getpgid(child.pid)
    try:
        assert pgid not in procs.sweep(set()), "a live spawner's group was swept"
        assert _alive_pids_in_group(pgid)
    finally:
        procs.kill_group(pgid, wait=True, proc=child)
        owner.kill()
        owner.wait()


def test_sweep_spares_own_process_group(tmp_path):
    """The sweep must never kill Fubala itself (own pid/pgid skipped)."""
    own = os.getpgrp()
    assert own not in procs.sweep(set())
    assert own not in procs.sweep(None)


# ---- 4. startup sweep ------------------------------------------------------


def test_startup_sweep_runs(tmp_path, monkeypatch):
    """`StoryRunner.start()` performs one immediate sweep before the loop."""
    runner = _make_runner(tmp_path)
    calls: list[set[str]] = []

    def fake_sweep(running: set[str] | None = None) -> list[int]:
        calls.append(set(running or ()))  # type: ignore[arg-type]
        return []

    monkeypatch.setattr(procs, "sweep", fake_sweep)
    # keep the real loop from also calling it on its first tick
    monkeypatch.setattr(procs, "sweep_interval", lambda: 10_000.0)

    async def scenario() -> list[set[str]]:
        await runner.start()
        try:
            return list(calls)
        finally:
            await runner.stop()

    observed = asyncio.run(scenario())
    assert observed, "start() did not run the proc sweep"


def test_sweep_interval_env_override(monkeypatch):
    monkeypatch.setenv("FUBALA_PROC_SWEEP_INTERVAL", "7")
    assert procs.sweep_interval() == 7.0
    monkeypatch.setenv("FUBALA_PROC_SWEEP_INTERVAL", "0")
    assert procs.sweep_interval() == procs.SWEEP_INTERVAL
    monkeypatch.setenv("FUBALA_PROC_SWEEP_INTERVAL", "not-a-number")
    assert procs.sweep_interval() == procs.SWEEP_INTERVAL
    monkeypatch.delenv("FUBALA_PROC_SWEEP_INTERVAL", raising=False)
    assert procs.sweep_interval() == 600.0


# ---- 5. ToolBox.run_command gets the same treatment ------------------------


def test_run_command_timeout_kills_group(tmp_path):
    """The coder's run_command times out (default cap 180s, args cap 60s) with
    the SAME group kill, and its backgrounded grandchild dies with it."""
    from app.agent_tools import ToolBox

    repo = init_repo(tmp_path / "repo3")
    ws = repo / ".fubala-worktrees" / "s-tool"
    ws.mkdir(parents=True)
    pidfile = ws / "pids"
    box = ToolBox(
        project_root=str(repo),
        workspace=str(ws),
        writable=True,
        allow_commands=("sh",),
        story_id="story-toolbox",
    )
    # run_command shlex-splits (no shell), so the spawning script goes to `sh -c`
    command = "sh -c " + _json_string(
        "sleep 300 & echo $! > " + str(pidfile) + "; sleep 300"
    )
    started = time.monotonic()
    result = box.execute(
        "run_command", '{"command": ' + _json_string(command) + ', "timeout": 1}'
    )
    elapsed = time.monotonic() - started
    assert "command timed out after 1s" in result
    assert elapsed < 15
    grandchild = int(pidfile.read_text().strip())
    _wait_gone(lambda: _pid_alive(grandchild))
    assert not procs.registered_groups("story-toolbox")


def _json_string(value: str) -> str:
    import json

    return json.dumps(value)


def test_run_command_env_is_marked_for_a_story(tmp_path):
    """The toolbox child env carries the marker + story id (so /proc can find
    it) while an unattributed toolbox stays unmarked."""
    from app.agent_tools import ToolBox

    repo = init_repo(tmp_path / "repo4")
    ws = repo / ".fubala-worktrees" / "s-mark"
    ws.mkdir(parents=True)
    box = ToolBox(
        project_root=str(repo),
        workspace=str(ws),
        writable=True,
        allow_commands=("printenv",),
        story_id="story-marker",
    )
    result = box.execute(
        "run_command",
        '{"command": "printenv ' + procs.MARKER + '"}',
    )
    assert "exit=0" in result
    assert "\n1\n" in result, "the marker value must reach the child"

    plain = ToolBox(
        project_root=str(repo),
        workspace=str(ws),
        writable=True,
        allow_commands=("printenv",),
    )
    unmarked = plain.execute(
        "run_command", '{"command": "printenv ' + procs.MARKER + '"}'
    )
    assert "exit=1" in unmarked, "an unattributed toolbox must stay unmarked"


# ---- kill primitive edge cases ---------------------------------------------


def test_kill_group_handles_dead_group() -> None:
    """A group that is already gone is a no-op, not an error."""
    child = subprocess.Popen(
        ["true"], start_new_session=True, stdout=subprocess.DEVNULL
    )
    child.wait(timeout=5)
    assert procs.kill_group(child.pid, wait=True) in (True, False)


def test_kill_group_escalates_to_sigkill(monkeypatch):
    """A grandchild that ignores SIGTERM still dies inside the grace."""
    blocker = subprocess.Popen(
        # TRAP SIGTERM and keep sleeping: only SIGKILL ends it
        ["sh", "-c", "trap '' TERM INT; sleep 300"],
        env=procs.mark_env(dict(os.environ), "escalate-story"),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pgid = os.getpgid(blocker.pid)
    try:
        start = time.monotonic()
        assert procs.kill_group(pgid, wait=True, grace=1.0)
        elapsed = time.monotonic() - start
        assert elapsed < 10
        _wait_gone(lambda: _alive_pids_in_group(pgid), timeout=5)
    finally:
        procs.kill_group(pgid, wait=False, proc=blocker)


def test_kill_group_reaps_the_direct_child(tmp_path):
    child = subprocess.Popen(
        ["sleep", "300"], start_new_session=True, stdout=subprocess.DEVNULL
    )
    pgid = os.getpgid(child.pid)
    procs.kill_group(pgid, wait=True, proc=child)
    assert child.poll() is not None


def test_unregister_clears_story_slot() -> None:
    procs.register("u-story", 4242)
    assert 4242 in procs.registered_groups("u-story")
    procs.unregister("u-story", 4242)
    assert procs.registered_groups("u-story") == set()


def test_mark_env_without_story_id_is_noop(monkeypatch) -> None:
    assert procs.mark_env({"A": "1"}, None) == {"A": "1"}
    # the None branch returns None only from a clean parent env; the suite
    # itself may run inside a marked verify tree, so scrub the markers first
    for var in (procs.MARKER, procs.STORY_ID_VAR, procs.SPAWNER_PID_VAR):
        monkeypatch.delenv(var, raising=False)
    assert procs.mark_env(None, None) is None
    # nested case: markers in the parent env are stripped, never inherited
    monkeypatch.setenv(procs.MARKER, "1")
    monkeypatch.setenv(procs.STORY_ID_VAR, "outer-story")
    stripped = procs.mark_env(None, None)
    assert stripped is not None
    assert procs.MARKER not in stripped
    assert procs.STORY_ID_VAR not in stripped


def test_mark_env_does_not_touch_os_environ() -> None:
    before = dict(os.environ)
    procs.mark_env({"A": "1"}, "leak-story")
    assert os.environ == before


def test_popen_registers_under_story(tmp_path):
    child, pgid = procs.popen(
        ["sleep", "300"], story_id="popen-story", stdout=subprocess.DEVNULL
    )
    try:
        assert pgid is not None and pgid in procs.registered_groups("popen-story")
        assert os.getpgid(child.pid) == pgid
    finally:
        procs.kill_group(pgid, wait=True, proc=child)
        procs.unregister("popen-story", pgid)
    assert procs.registered_groups("popen-story") == set()


def test_popen_without_story_id_not_marked():
    """No story id: the child still gets its OWN group (so its spawn site can
    kill it on timeout) but the pgid is never registered — an unattributable
    group must stay invisible to the sweep."""
    child, pgid = procs.popen(["sh", "-c", "sleep 300"], stdout=subprocess.DEVNULL)
    try:
        assert pgid is not None
        assert os.getpgid(child.pid) == pgid
        assert not any(
            pgid in groups for groups in procs._REGISTRY.values()
        ), "an unattributed group must not be registered"
        env_marker = Path(f"/proc/{child.pid}/environ").read_bytes()
        assert procs.MARKER.encode() not in env_marker
    finally:
        procs.kill_group(pgid, wait=True, proc=child)


# ---- signal hygiene --------------------------------------------------------


def test_no_marker_in_server_env(monkeypatch) -> None:
    """The module must never ADD markers to the server's own environment.

    (When this suite itself runs inside a Fubala verify tree the markers are
    legitimately INHERITED — that is the mechanism working, not a leak — so the
    assertion targets what the module controls: mark_env/register leave
    os.environ alone, and a clean env stays clean.)"""
    monkeypatch.delenv(procs.MARKER, raising=False)
    monkeypatch.delenv(procs.STORY_ID_VAR, raising=False)
    procs.mark_env({"A": "1"}, "env-story")
    procs.register("env-story", 4321)
    assert procs.MARKER not in os.environ
    assert procs.STORY_ID_VAR not in os.environ
    procs.unregister("env-story", 4321)


def test_signal_group_ignores_missing_group() -> None:
    procs._signal_group(2**30, signal.SIGTERM)  # no raise = pass


# ---- 6. escapees (setsid/nohup) are reaped when the run ends ------------------


@pytest.fixture
def reports():
    got: list[tuple[str, str, list]] = []
    procs.set_reporter(lambda sid, reason, entries: got.append((sid, reason, entries)))
    yield got
    procs.set_reporter(None)


def _story_pids(story_id: str) -> set[int]:
    """Live non-zombie pids whose env carries `story_id`'s marker."""
    found: set[int] = set()
    for pid, state, _pgid in procs._iter_proc_stat():
        if state != "Z" and procs._environ_of(pid).get(procs.STORY_ID_VAR) == story_id:
            found.add(pid)
    return found


def _wait_story_gone(story_id: str) -> None:
    _wait_gone(lambda: _story_pids(story_id))


# starts a detached sleep, records its pid and returns once it IS the sleep
# (setsid/nohup exec in place of the backgrounded job, so $! is its pid)
ESCAPEE = (
    "setsid nohup sleep 600 >/dev/null 2>&1 & echo $! >{pidfile}; "
    'while [ "$(cat /proc/$!/comm 2>/dev/null)" != sleep ]; do sleep 0.02; done'
)


def _assert_reported(reports, story_id: str, reason: str, pid: int) -> None:
    entries = [e for sid, r, es in reports if sid == story_id and r == reason for e in es]
    hits = [e for e in entries if e["pid"] == pid]
    assert hits, (reason, pid, reports)
    assert "sleep" in hits[0]["command"] and isinstance(hits[0]["age_s"], int)


@pytest.mark.parametrize("timeout,reason", [(30.0, "round_end"), (1.5, "timeout")])
def test_verify_reaps_the_escapee(tmp_path, reports, timeout, reason):
    repo = init_repo(tmp_path)
    ws = repo / ".fubala-worktrees" / "esc"
    ws.mkdir(parents=True)
    pidfile = ws / "pid"
    command = ESCAPEE.format(pidfile=pidfile)
    if reason == "timeout":
        command += "; sleep 300"
    rc, _ = _run_verify_command(ws, command, timeout, str(repo), "story-esc-verify")
    assert rc == (124 if reason == "timeout" else 0)
    pid = int(pidfile.read_text())
    _wait_story_gone("story-esc-verify")
    assert not _pid_alive(pid)
    _assert_reported(reports, "story-esc-verify", reason, pid)


@pytest.mark.parametrize("timeout,reason", [(30, "round_end"), (1, "timeout")])
def test_run_command_reaps_the_escapee(tmp_path, reports, timeout, reason):
    from app.agent_tools import ToolBox

    repo = init_repo(tmp_path / "repo-rc")
    ws = repo / ".fubala-worktrees" / "esc"
    ws.mkdir(parents=True)
    pidfile = ws / "pid"
    script = ESCAPEE.format(pidfile=pidfile) + ("; sleep 300" if reason == "timeout" else "")
    box = ToolBox(
        project_root=str(repo),
        workspace=str(ws),
        writable=True,
        allow_commands=("sh",),
        story_id="story-esc-tool",
    )
    command = "sh -c " + _json_string(script)
    box.execute(
        "run_command",
        '{"command": ' + _json_string(command) + f', "timeout": {timeout}' + "}",
    )
    pid = int(pidfile.read_text())
    _wait_story_gone("story-esc-tool")
    assert not _pid_alive(pid)
    _assert_reported(reports, "story-esc-tool", reason, pid)


def _escaping_claude(tmp_path: Path, inner_mode: str, pidfile: Path) -> Path:
    """A `claude` that leaves a detached sleep behind, then behaves like the
    fake in `inner_mode`."""
    from test_headless import install_fake_claude

    inner = install_fake_claude(tmp_path, inner_mode) / "claude"
    bindir = tmp_path / f"bin-esc-{inner_mode}"
    bindir.mkdir()
    script = bindir / "claude"
    script.write_text(
        "#!/bin/sh\n" + ESCAPEE.format(pidfile=pidfile) + f"\nexec {inner} \"$@\"\n"
    )
    script.chmod(0o755)
    return bindir


@pytest.mark.parametrize(
    "inner,timeout,reason", [("ok", 30.0, "round_end"), ("sleep", 2.0, "timeout")]
)
def test_headless_round_reaps_the_escapee(tmp_path, reports, inner, timeout, reason):
    from test_headless import _init_repo, make_config, on_path

    from app import headless as hx

    wt = Path(_init_repo(tmp_path / "hl"))
    pidfile = tmp_path / "pid"
    bindir = _escaping_claude(tmp_path, inner, pidfile)
    with on_path(bindir):
        hx.run_code_stage(
            story_id="story-esc-hl",
            worktree=wt,
            prompt="go",
            config=make_config(timeout_seconds=timeout),
        )
    pid = int(pidfile.read_text())
    _wait_story_gone("story-esc-hl")
    assert not _pid_alive(pid)
    _assert_reported(reports, "story-esc-hl", reason, pid)


def test_cancel_reaps_the_escapee(tmp_path, reports):
    runner = _make_runner(tmp_path)
    repo = init_repo(tmp_path / "repo-cancel-esc")
    ws = repo / ".fubala-worktrees" / "esc"
    ws.mkdir(parents=True)
    pidfile = ws / "pid"
    story_id = "story-esc-cancel"

    async def scenario() -> None:
        task = asyncio.ensure_future(
            asyncio.to_thread(
                _run_verify_command,
                ws,
                ESCAPEE.format(pidfile=pidfile) + "; sleep 300",
                300.0,
                str(repo),
                story_id,
            )
        )
        runner._running.add(story_id)
        runner._tasks[story_id] = task
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (
            pidfile.exists() and pidfile.read_text().strip()
        ):
            await asyncio.sleep(0.05)
        await asyncio.sleep(0)  # let the loop breathe; the script gates on /proc/comm
        assert await runner.cancel(story_id)

    asyncio.run(scenario())
    pid = int(pidfile.read_text())
    _wait_story_gone(story_id)
    assert not _pid_alive(pid)
    _assert_reported(reports, story_id, "cancel", pid)


def test_reaping_run_a_leaves_run_b_alone(tmp_path):
    """Two runs of the SAME story: A's end-of-run reap must not touch B."""
    story = "story-two-runs"
    env = {**os.environ}
    a, a_pgid = procs.popen(
        ["sh", "-c", "setsid sleep 600 & wait"], story_id=story, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    b, b_pgid = procs.popen(
        ["sh", "-c", "setsid sleep 601 & wait"], story_id=story, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip

    def sleeper(run_id: str) -> int | None:
        for pid, state, _ in procs._iter_proc_stat():
            e = procs._environ_of(pid)
            if state != "Z" and e.get(procs.RUN_ID_VAR) == run_id and "sleep" in Path(
                f"/proc/{pid}/comm"
            ).read_text():
                return pid
        return None

    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (
            sleeper(a.fubala_run_id) and sleeper(b.fubala_run_id)
        ):
            time.sleep(0.02)
        a_sleep, b_sleep = sleeper(a.fubala_run_id), sleeper(b.fubala_run_id)
        assert a_sleep and b_sleep
        killed = procs.reap_run(story, a.fubala_run_id, a_pgid, reason="round_end")
        assert a_sleep in {k["pid"] for k in killed}
        _wait_gone(lambda: _pid_alive(a_sleep))
        assert _pid_alive(b_sleep), "run B's process was reaped with run A"
    finally:
        procs.reap_run(story, b.fubala_run_id, b_pgid)
        procs.kill_group(a_pgid, wait=False, proc=a)
        procs.kill_group(b_pgid, wait=False, proc=b)
        procs.unregister(story, a_pgid)
        procs.unregister(story, b_pgid)


def test_sweep_kills_the_leftovers_of_a_finished_run(tmp_path, reports):
    """An escapee of a run this server already finished (and failed to reap)
    is a leftover: the sweep kills it and reports reason `sweep`."""
    story = "story-sweep-left"
    child, pgid = procs.popen(
        ["sh", "-c", "setsid sleep 600 & wait"], story_id=story,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    run_id = child.fubala_run_id
    try:
        deadline = time.monotonic() + 10
        escapee = None
        while time.monotonic() < deadline and escapee is None:
            for pid, state, grp in procs._iter_proc_stat():
                if state != "Z" and grp != pgid and procs._environ_of(pid).get(
                    procs.RUN_ID_VAR
                ) == run_id and "sleep" in Path(f"/proc/{pid}/comm").read_text():
                    escapee = pid
            time.sleep(0.02)
        assert escapee
        # while the run is active (registered), the sweep spares everything
        assert procs.sweep({story}) == []
        assert _pid_alive(escapee)
        # the run ends without reaping: group gone, registration dropped
        procs.kill_group(pgid, wait=True, proc=child)
        procs.unregister(story, pgid)
        with procs._LOCK:
            procs._ACTIVE_RUNS.discard(run_id)
        killed = procs.sweep(set())
        assert killed, "the leftover escapee was not swept"
        _wait_gone(lambda: _pid_alive(escapee))
        _assert_reported(reports, story, "sweep", escapee)
    finally:
        procs.kill_group(pgid, wait=False, proc=child)
