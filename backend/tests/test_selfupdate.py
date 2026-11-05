"""Tests for the self-update helper and its spawner.

All network/OS side effects are patched so tests never really spawn uvicorn
or signal anything — except `ensure_exited`, which uses a real sleeper child
to prove the old process actually dies.
"""

import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from app import selfupdate


def _entry_point_tree(tmp_path):
    """A backend dir whose app package HAS a __main__.py (`python -m app` works)."""
    backend_dir = tmp_path / "backend"
    (backend_dir / "app").mkdir(parents=True)
    (backend_dir / "app" / "__main__.py").write_text("", encoding="utf-8")
    return str(backend_dir)


def _legacy_tree(tmp_path):
    """A backend dir without app/__main__.py — a rolled-back (pre-merge) tree."""
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir(parents=True)
    return str(backend_dir)


def _fake_probe_run(calls: list, ok_candidates: tuple[str, ...] = ("python", "uv")):
    """Patch subprocess.run so the interpreter probe answers deterministically.

    `calls` records every probe so tests can assert which candidates were
    tried; ok_candidates decides which prefix succeeds ("python" = our
    sys.executable, "uv" = the uv fallback)."""

    class FakeCompleted:
        returncode = 0

    def fake_run(cmd, **kwargs):
        if cmd[-2:] == ["-c", "import uvicorn"]:
            which = "uv" if cmd[0] == "uv" else "python"
            calls.append(which)
            if which not in ok_candidates:
                return SimpleNamespace(returncode=1)
            return FakeCompleted()
        raise OSError("unexpected subprocess call")

    return fake_run


def _with_uv(monkeypatch):
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    # the dev machine exports FUBALA_HOST/FUBALA_PORT for its own server;
    # the helper copies os.environ, so tests must start from a clean one
    monkeypatch.delenv("FUBALA_HOST", raising=False)
    monkeypatch.delenv("FUBALA_PORT", raising=False)


def test_server_command_prefers_entry_point(monkeypatch, tmp_path):
    """With app/__main__.py present the server launch is the same entry point
    as `python -m app` — no host/port flags of its own; the bind comes from
    FUBALA_HOST/FUBALA_PORT in the spawned env."""
    backend_dir = _entry_point_tree(tmp_path)
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _fake_probe_run(calls))
    _with_uv(monkeypatch)

    argv, env = selfupdate._server_command(
        backend_dir, 8123, "/tmp/db.db", host="0.0.0.0"
    )
    assert argv == [sys.executable, "-m", "app"]
    assert env["FUBALA_HOST"] == "0.0.0.0"
    assert env["FUBALA_PORT"] == "8123"
    assert "--host" not in argv and "--port" not in argv


def test_server_command_falls_back_to_uvicorn_without_entry_point(
    monkeypatch, tmp_path
):
    """Rollback safety: a pre-merge tree without app/__main__.py cannot run
    `-m app`; the helper falls back to today's uvicorn argv with the same
    host/port values (and no FUBALA_HOST/FUBALA_PORT injection)."""
    backend_dir = _legacy_tree(tmp_path)
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _fake_probe_run(calls))
    _with_uv(monkeypatch)

    argv, env = selfupdate._server_command(
        backend_dir, 8123, "/tmp/db.db", host="0.0.0.0"
    )
    assert argv[:4] == [sys.executable, "-m", "uvicorn", "app.main:app"]
    assert argv[4:6] == ["--host", "0.0.0.0"]
    assert argv[6:8] == ["--port", "8123"]
    assert "FUBALA_HOST" not in env and "FUBALA_PORT" not in env


def test_server_command_uv_candidate_prefers_entry_point(monkeypatch, tmp_path):
    """The uv fallback candidate gets the same treatment: `-m app` with
    FUBALA_HOST/FUBALA_PORT when the entry point exists."""
    backend_dir = _entry_point_tree(tmp_path)
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _fake_probe_run(calls, ok_candidates=("uv",)))
    _with_uv(monkeypatch)

    argv, env = selfupdate._server_command(
        backend_dir, 8000, "/tmp/db.db", host="192.168.1.10"
    )
    assert argv[:3] == ["uv", "run", "--project"]
    assert argv[-2:] == ["-m", "app"]
    assert env["FUBALA_HOST"] == "192.168.1.10"
    assert env["FUBALA_PORT"] == "8000"


def test_server_command_uv_fallback_without_entry_point(monkeypatch, tmp_path):
    """uv candidate + no entry point -> uv runs uvicorn with --host/--port."""
    backend_dir = _legacy_tree(tmp_path)
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _fake_probe_run(calls, ok_candidates=("uv",)))
    _with_uv(monkeypatch)

    argv, env = selfupdate._server_command(
        backend_dir, 9001, "/tmp/db.db", host="0.0.0.0"
    )
    assert argv[:3] == ["uv", "run", "--project"]
    assert argv[argv.index("--host") + 1] == "0.0.0.0"
    assert argv[argv.index("--port") + 1] == "9001"
    assert "FUBALA_HOST" not in env


def test_server_command_last_resort_keeps_bind(monkeypatch, tmp_path):
    """No probe succeeds: assume our interpreter anyway, with the entry point
    when the tree has one (bind still carried via FUBALA_HOST/FUBALA_PORT)."""
    backend_dir = _entry_point_tree(tmp_path)
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _fake_probe_run(calls, ok_candidates=()))
    monkeypatch.setattr("shutil.which", lambda name: None)

    argv, env = selfupdate._server_command(
        backend_dir, 8123, "/tmp/db.db", host="0.0.0.0"
    )
    assert argv == [sys.executable, "-m", "app"]
    assert env["FUBALA_HOST"] == "0.0.0.0" and env["FUBALA_PORT"] == "8123"


def test_port_in_use_and_healthy_use_probe_host(monkeypatch):
    """Wildcard binds (0.0.0.0 / ::) are probed on loopback, other hosts as-is."""
    seen: list[tuple[str, int]] = []

    class FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

        def connect_ex(self, addr):
            seen.append(addr)
            return 0  # "in use"

    class FakeSockModule:
        AF_INET = None
        SOCK_STREAM = None

        @staticmethod
        def socket(family, type):
            return FakeSock()

    import socket as socket_module

    monkeypatch.setattr(socket_module, "socket", FakeSockModule.socket)
    assert selfupdate._port_in_use(8000, "0.0.0.0") is True
    assert selfupdate._port_in_use(8000, "192.168.1.10") is True
    assert [a[0] for a in seen] == ["127.0.0.1", "192.168.1.10"]

    # _healthy dials the same probe host
    captured: dict[str, str] = {}

    class FakeRes:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout=None):
        captured["url"] = url
        return FakeRes()

    monkeypatch.setattr(selfupdate.urllib.request, "urlopen", fake_urlopen)
    assert selfupdate._healthy(8000, "0.0.0.0") is True
    assert captured["url"].startswith("http://127.0.0.1:8000")
    assert selfupdate._healthy(8000, "192.168.1.10") is True
    assert captured["url"].startswith("http://192.168.1.10:8000")


def test_spawn_self_update_passes_settings_host_and_port(monkeypatch):
    """AC 2: FUBALA_HOST/FUBALA_PORT (Settings) flow into the detached
    helper's argv; no real spawn.

    We must NOT run the FastAPI background task (_step_aside SIGTERMs the
    test process) — BackgroundTasks is only passed through, never executed."""
    from fastapi import BackgroundTasks

    from app.routers import stories

    monkeypatch.setenv("FUBALA_HOST", "0.0.0.0")
    monkeypatch.setenv("FUBALA_PORT", "8123")
    monkeypatch.setattr(stories.os, "getpid", lambda: 4242)

    spawned: list[dict] = []

    def fake_popen(argv, **kwargs):
        spawned.append({"argv": argv, "kwargs": kwargs})
        return SimpleNamespace(pid=99999)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    info = {
        "port": None,  # forces the get_settings() fallback -> FUBALA_PORT
        "host": None,  # forces the get_settings() fallback -> FUBALA_HOST
        "old_pid": 4242,
        "repo": "/repo",
        "db": "/repo/backend/data/x.db",
        "story": "S-42",
        "merge_sha": "abc123",
        "backend_dir": "/repo/backend",
    }
    stories._spawn_self_update(info, BackgroundTasks())

    assert len(spawned) == 1
    argv = spawned[0]["argv"]
    assert argv[argv.index("--host") + 1] == "0.0.0.0"
    assert argv[argv.index("--port") + 1] == "8123"
    assert argv[argv.index("--old-pid") + 1] == "4242"
    assert spawned[0]["kwargs"].get("start_new_session") is True
    assert spawned[0]["kwargs"]["cwd"] == "/repo/backend"


def _orphan_sleeper(pidfile) -> int:
    """Double-fork a `time.sleep` orphan and return its pid.

    The sleeper is NOT our child: like the real old server it is orphaned
    (init reaps it) and runs in its own session. A plain child would linger
    as an un-reaped zombie, on which os.kill(pid, 0) keeps succeeding forever.
    """
    forker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os, sys, time\n"
            "pid = os.fork()\n"
            "if pid:\n"
            "    open(sys.argv[1], 'w').write(str(pid))\n"
            "    sys.exit(0)\n"  # parent leaves -> grandchild is orphaned
            "os.setsid()\n"
            "time.sleep(120)\n",
            str(pidfile),
        ]
    )
    # process-local join: the forker is a direct Popen child that exits at
    # once; the bound is a kill-switch, not a pipeline-progress gate
    forker.wait(timeout=30)
    return int(pidfile.read_text())


def _kill_quietly(pid: int) -> None:
    """SIGKILL a pid, swallowing the already-gone case."""
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_ensure_exited_kills_sleeper(tmp_path, monkeypatch):
    """Old process really dies: alive -> SIGTERM grace -> SIGKILL -> gone.

    The /proc stranger guard is neutralized here: on Linux the sleeper's
    cmdline has no "uvicorn" in it, so the guard would (correctly) treat it
    as a recycled pid and spare it. This test is about the escalation path,
    so the guard says "ours" unconditionally — same as on macOS, where there
    is no /proc and _is_stranger is always False."""
    monkeypatch.setattr(selfupdate, "_is_stranger", lambda pid: False)
    pid = _orphan_sleeper(tmp_path / "sleeper.pid")
    try:
        # timeout_s is under test here (kill-switch bound), not a wait
        assert selfupdate.ensure_exited(pid, grace_s=0.2, timeout_s=2.0)
        # and it is truly gone, not a zombie: os.kill must raise
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        _kill_quietly(pid)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="/proc is Linux-only"
)
def _wait_cmdline(pid: int, needle: bytes, timeout_s: float = 30.0) -> bool:
    """Wait until the child's /proc cmdline actually reflects its own exec.

    Popen may return before the child has execed (posix_spawn); until then
    /proc/<pid>/cmdline still holds the parent pytest's argv, which would
    flake the stranger detection below on a loaded machine."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                if needle in fh.read():
                    return True
        except OSError:
            pass  # process gone or cmdline not ready yet; keep polling
        time.sleep(0.05)
    return False


def test_is_stranger_linux():
    """The guard reads /proc/<pid>/cmdline: no "uvicorn" -> stranger (the pid
    was recycled by an unrelated program), "uvicorn" in the cmdline -> ours."""
    plain = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"]
    )
    serverish = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)  # uvicorn"]
    )
    try:
        assert _wait_cmdline(plain.pid, b"sleep"), "plain child never execed"
        assert _wait_cmdline(serverish.pid, b"uvicorn"), (
            "serverish child never execed"
        )
        assert selfupdate._is_stranger(plain.pid) is True
        assert selfupdate._is_stranger(serverish.pid) is False
    finally:
        for proc in (plain, serverish):
            proc.kill()
            # process-local join: proc is a direct Popen child that has
            # already exited by construction; the bound is a kill-switch,
            # not a pipeline-progress gate
            proc.wait(timeout=30)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="/proc is Linux-only"
)
def test_ensure_exited_spares_recycled_pid(tmp_path):
    """A pid that now belongs to another program must be left alone: the guard
    reports the old server as exited without signalling the newcomer."""
    pid = _orphan_sleeper(tmp_path / "sleeper.pid")
    try:
        assert selfupdate.ensure_exited(pid, grace_s=0.2, timeout_s=2.0)
        # spared, not killed: the pid must still exist right now (no polling,
        # which could pass on timing alone)
        os.kill(pid, 0)
    finally:
        _kill_quietly(pid)


def test_ensure_exited_dead_pid_is_success():
    """A pid that is already gone must not fail (or delay) the update."""
    assert selfupdate.ensure_exited(-1, grace_s=0.0, timeout_s=0.5) is True


def test_main_order_ensure_exited_before_start(monkeypatch, tmp_path):
    """`main` must guarantee the old process is gone BEFORE it starts the new
    server, and must pass the bind host through to both start attempts."""
    calls: list[str] = []

    proc = SimpleNamespace(poll=lambda: None, pid=1234)

    monkeypatch.setattr(
        selfupdate, "wait_port_free", lambda port, host: (
            calls.append(f"wait_port_free:{host}") or True
        )
    )
    monkeypatch.setattr(
        selfupdate,
        "ensure_exited",
        lambda pid, grace_s=1.0, timeout_s=5.0: calls.append(f"ensure_exited:{pid}") or True,
    )
    monkeypatch.setattr(
        selfupdate,
        "start_server",
        lambda backend_dir, port, db, host: calls.append(f"start_server:{host}") or proc,
    )
    monkeypatch.setattr(
        selfupdate, "wait_healthy", lambda port, proc, host: (
            calls.append(f"wait_healthy:{host}") or True
        )
    )
    monkeypatch.setattr(
        selfupdate,
        "record_event",
        lambda db, story, kind, data: calls.append(f"event:{data}"),
    )

    rc = selfupdate.main(
        [
            "--port", "8000",
            "--host", "0.0.0.0",
            "--old-pid", "777",
            "--repo", str(tmp_path),
            "--db", str(tmp_path / "x.db"),
            "--story", "S-1",
            "--merge-sha", "deadbee",
            "--backend-dir", str(tmp_path),
        ]
    )
    assert rc == 0
    assert calls[0].startswith("wait_port_free:")
    assert calls[1] == "ensure_exited:777"
    assert calls[2] == "start_server:0.0.0.0"
    assert calls[3] == "wait_healthy:0.0.0.0"
    assert calls[1].split(":")[0] == "ensure_exited" and calls[2].split(":")[0] == "start_server"
    # index-wise the guarantee holds: ensure_exited strictly before start_server
    assert calls.index("ensure_exited:777") < calls.index("start_server:0.0.0.0")


def _repo_with_backend_and_frontend(tmp_path, merge_sha_output: list):
    """A real throwaway git repo whose commits touch backend/ and frontend/.

    `merge_sha_output` collects (sha, changed-paths) for every commit so the
    test can ask merge_touched_frontend about shas of known shape.
    """
    import shutil as shutil_module

    if shutil_module.which("git") is None:
        pytest.skip("git not installed")

    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo),
           "-c", "user.name=t", "-c", "user.email=t@example.com"]

    def git_run(*argv):
        res = subprocess.run(git + list(argv), capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        return res.stdout.strip()

    git_run("init", "-q", "--initial-branch=main")
    (repo / "backend" / "app").mkdir(parents=True)
    (repo / "frontend" / "src").mkdir(parents=True)
    (repo / "backend" / "app" / "main.py").write_text("1\n", encoding="utf-8")
    (repo / "frontend" / "src" / "main.tsx").write_text("1\n", encoding="utf-8")
    git_run("add", ".")
    git_run("commit", "-q", "-m", "base")

    # a plain (non-merge) commit touching only backend/
    (repo / "backend" / "app" / "main.py").write_text("2\n", encoding="utf-8")
    git_run("add", ".")
    git_run("commit", "-q", "-m", "backend only")
    merge_sha_output.append(("backend-only plain commit", git_run("rev-parse", "HEAD")))

    # a plain commit touching frontend/ (non-merge shape, still detected)
    (repo / "frontend" / "src" / "main.tsx").write_text("2\n", encoding="utf-8")
    git_run("add", ".")
    git_run("commit", "-q", "-m", "frontend plain")
    merge_sha_output.append(("frontend plain commit", git_run("rev-parse", "HEAD")))

    # a merge whose FIRST-parent diff touches frontend/ -> detected
    git_run("checkout", "-q", "-b", "feature")
    (repo / "frontend" / "src" / "main.tsx").write_text("3\n", encoding="utf-8")
    git_run("add", ".")
    git_run("commit", "-q", "-m", "frontend on branch")
    git_run("checkout", "-q", "main")
    git_run("merge", "-q", "--no-ff", "-m", "merge frontend", "feature")
    merge_sha_output.append(("merge touching frontend", git_run("rev-parse", "HEAD")))

    # a merge whose first-parent diff is backend-only even though the branch
    # itself carried frontend files before -> NOT detected (first-parent rule)
    git_run("checkout", "-q", "-b", "backendwork")
    (repo / "backend" / "app" / "main.py").write_text("3\n", encoding="utf-8")
    git_run("add", ".")
    git_run("commit", "-q", "-m", "backend on branch")
    git_run("checkout", "-q", "main")
    git_run("merge", "-q", "--no-ff", "-m", "merge backend", "backendwork")
    merge_sha_output.append(("merge touching only backend", git_run("rev-parse", "HEAD")))

    return str(repo)


def test_merge_touched_frontend_against_real_repo(tmp_path):
    """Detection: True iff the first-parent diff of the merge names anything
    under frontend/ — checked against real git commits of every shape."""
    commits: list[tuple[str, str]] = []
    repo = _repo_with_backend_and_frontend(tmp_path, commits)
    by_label = dict(commits)

    assert selfupdate.merge_touched_frontend(
        repo, by_label["backend-only plain commit"]
    ) is False
    assert selfupdate.merge_touched_frontend(
        repo, by_label["frontend plain commit"]
    ) is True
    assert selfupdate.merge_touched_frontend(
        repo, by_label["merge touching frontend"]
    ) is True
    assert selfupdate.merge_touched_frontend(
        repo, by_label["merge touching only backend"]
    ) is False

    # a bogus sha / broken repo must never crash or claim a frontend change
    assert selfupdate.merge_touched_frontend(repo, "0" * 40) is False
    assert selfupdate.merge_touched_frontend(str(tmp_path / "nope"), "abc") is False


def test_merge_touched_frontend_uses_first_parent_argv():
    """The git call mirrors the revert semantics: -m --first-parent -r."""
    argvs: list = []

    def fake_run(cmd, **kwargs):
        argvs.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert selfupdate.merge_touched_frontend("/repo", "beef", run=fake_run) is False
    cmd = argvs[0]
    assert cmd[:3] == ["git", "-C", "/repo"]
    assert cmd[3] == "diff-tree"
    for flag in ("--no-commit-id", "--name-only", "-r", "-m", "--first-parent"):
        assert flag in cmd
    assert cmd[-1] == "beef"


def _patch_main_plumbing(monkeypatch, tmp_path, calls, proc):
    """Fake the OS/network edges of main(); everything interesting records."""
    monkeypatch.setattr(selfupdate, "wait_port_free", lambda port, host: True)
    monkeypatch.setattr(
        selfupdate, "ensure_exited",
        lambda pid, **k: calls.append(f"ensure_exited:{pid}") or True,
    )
    monkeypatch.setattr(
        selfupdate, "start_server",
        lambda backend_dir, port, db, host: calls.append("start_server") or proc,
    )
    monkeypatch.setattr(
        selfupdate, "wait_healthy",
        lambda port, proc, host: calls.append("wait_healthy") or True,
    )
    monkeypatch.setattr(
        selfupdate, "record_event",
        lambda db, story, kind, data: calls.append((kind, data)),
    )
    monkeypatch.setattr(
        selfupdate, "revert_merge",
        lambda repo, sha: calls.append("revert_merge") or True,
    )
    return [
        "--port", "8000",
        "--host", "0.0.0.0",
        "--old-pid", "777",
        "--repo", str(tmp_path / "repo"),
        "--db", str(tmp_path / "x.db"),
        "--story", "S-1",
        "--merge-sha", "deadbeef",
        "--backend-dir", str(tmp_path / "repo" / "backend"),
    ]


def test_main_builds_frontend_before_start_server(monkeypatch, tmp_path):
    """AC 1: a merge touching frontend/ makes main() rebuild dist (npm steps,
    cwd = sibling frontend dir) strictly after ensure_exited and strictly
    BEFORE the new server is started."""
    backend_dir = _entry_point_tree(tmp_path)  # tmp_path/backend/app/__main__.py
    frontend_dir = tmp_path / "frontend"
    (frontend_dir / "src").mkdir(parents=True)
    calls: list = []
    proc = SimpleNamespace(poll=lambda: None, pid=1234)

    argv = _patch_main_plumbing(monkeypatch, tmp_path, calls, proc)
    argv[argv.index("--backend-dir") + 1] = backend_dir

    monkeypatch.setattr(selfupdate, "merge_touched_frontend", lambda repo, sha: True)

    build_calls: list = []
    real_build = selfupdate.build_frontend

    def fake_run(cmd, **kwargs):
        build_calls.append((tuple(cmd), kwargs.get("cwd")))
        return SimpleNamespace(returncode=0, stdout="built\n", stderr="")

    def recording_build(bd):
        calls.append("build_frontend")
        return real_build(bd, run=fake_run)

    monkeypatch.setattr(selfupdate, "build_frontend", recording_build)

    rc = selfupdate.main(argv)
    assert rc == 0

    # the build ran, with both steps in the sibling frontend dir
    assert [c[0] for c in build_calls] == [
        ("npm", "ci"), ("npm", "run", "build")
    ]
    assert {c[1] for c in build_calls} == {str(frontend_dir)}

    order = [c if isinstance(c, str) else "event" for c in calls]
    assert order.index("ensure_exited:777") < order.index("build_frontend")
    assert order.index("build_frontend") < order.index("start_server")
    assert "revert_merge" not in order


def test_main_skips_build_when_merge_untouched_frontend(monkeypatch, tmp_path):
    """AC 2: a backend-only merge never builds the frontend."""
    calls: list = []
    proc = SimpleNamespace(poll=lambda: None, pid=1234)
    argv = _patch_main_plumbing(monkeypatch, tmp_path, calls, proc)

    monkeypatch.setattr(selfupdate, "merge_touched_frontend", lambda repo, sha: False)

    def must_not_build(backend_dir):
        calls.append("build_frontend")
        raise AssertionError("build_frontend must not run for a backend-only merge")

    monkeypatch.setattr(selfupdate, "build_frontend", must_not_build)

    rc = selfupdate.main(argv)
    assert rc == 0
    assert [c if isinstance(c, str) else "event" for c in calls] == [
        "ensure_exited:777", "start_server", "wait_healthy", "event",
    ]
    assert calls[-1][0] == "self_update" and calls[-1][1]["applied"] is True


def test_build_frontend_missing_sibling_is_a_skip(tmp_path, caplog):
    """No frontend/ next to backend/ -> skip (never revert a merge over it)."""
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()

    def must_not_run(cmd, **kwargs):
        raise AssertionError("no npm should run without a frontend dir")

    ok, err = selfupdate.build_frontend(str(backend_dir), run=must_not_run)
    assert ok is True and err == ""


def test_build_frontend_success_replaces_dist(tmp_path):
    """A good build leaves the fresh dist and deletes the move-aside backup."""
    frontend_dir = tmp_path / "frontend"
    dist = frontend_dir / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("old", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        assert kwargs.get("cwd") == str(frontend_dir)
        assert kwargs.get("timeout") == selfupdate.FRONTEND_BUILD_TIMEOUT_S
        if cmd[1:3] == ["run", "build"]:
            # dist was moved aside before the build; the build makes a new one
            dist.mkdir(parents=True, exist_ok=True)
            (dist / "index.html").write_text("new", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    ok, err = selfupdate.build_frontend(str(tmp_path / "backend"), run=fake_run)
    assert ok is True and err == ""
    assert (dist / "index.html").read_text(encoding="utf-8") == "new"
    assert not (frontend_dir / ".dist.selfupdate-bak").exists()


def test_main_failed_build_reverts_without_first_start(monkeypatch, tmp_path):
    """AC 3: failed build -> no first start, one start (post-revert), revert
    runs, the self_update event carries the build error and output, and the
    OLD dist is what the rollback server ends up serving."""
    frontend_dir = tmp_path / "frontend"
    dist = frontend_dir / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("STALE-BUT-SERVED", encoding="utf-8")

    calls: list = []
    starts = {"n": 0}
    proc = SimpleNamespace(poll=lambda: None, pid=1234)
    argv = _patch_main_plumbing(monkeypatch, tmp_path, calls, proc)
    argv[argv.index("--backend-dir") + 1] = str(tmp_path / "backend")

    monkeypatch.setattr(selfupdate, "merge_touched_frontend", lambda repo, sha: True)

    def failing_run(cmd, **kwargs):
        if cmd[1:3] == ["run", "build"]:
            # vite can empty dist before dying — the nasty real-world case.
            # dist was moved aside, so this recreates a PARTIAL one.
            dist.mkdir(parents=True, exist_ok=True)
            (dist / "index.html").write_text("half-written", encoding="utf-8")
            return SimpleNamespace(returncode=1, stdout="", stderr="SyntaxError: boom")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    real_build = selfupdate.build_frontend
    monkeypatch.setattr(
        selfupdate, "build_frontend",
        lambda bd: calls.append("build_frontend")
        or real_build(bd, run=failing_run),
    )

    def counting_start(backend_dir, port, db, host):
        starts["n"] += 1
        calls.append("start_server")
        return proc

    monkeypatch.setattr(selfupdate, "start_server", counting_start)

    rc = selfupdate.main(argv)
    assert rc == 0

    assert starts["n"] == 1  # only the post-revert start
    order = [c if isinstance(c, str) else "event" for c in calls]
    assert order == [
        "ensure_exited:777", "build_frontend", "revert_merge",
        "start_server", "wait_healthy", "event",
    ]

    events = [c for c in calls if isinstance(c, tuple)]
    assert len(events) == 1
    kind, data = events[0]
    assert kind == "self_update"
    assert data["applied"] is False and data["reverted"] is True
    assert data["merge"] == "deadbeef"
    assert data["error"].startswith("frontend build failed")
    assert "SyntaxError: boom" in data["error"]

    # the old dist survived the failed build, and no backup lingers
    assert (dist / "index.html").read_text(encoding="utf-8") == "STALE-BUT-SERVED"
    assert not (frontend_dir / ".dist.selfupdate-bak").exists()


def test_main_rollback_reuses_host(monkeypatch, tmp_path):
    """If the first start is unhealthy, the rollback binds the SAME host."""
    calls: list[str] = []
    # terminate/wait/kill are real stop_server's fallbacks when killpg fails
    proc = SimpleNamespace(
        poll=lambda: None,
        pid=1234,
        terminate=lambda: None,
        wait=lambda timeout=None: None,
        kill=lambda: None,
    )
    starts = {"n": 0}

    def fake_start(backend_dir, port, db, host):
        starts["n"] += 1
        calls.append(f"start:{host}")
        return proc

    monkeypatch.setattr(selfupdate, "wait_port_free", lambda port, host: True)
    monkeypatch.setattr(selfupdate, "ensure_exited", lambda pid: True)
    monkeypatch.setattr(selfupdate, "start_server", fake_start)
    monkeypatch.setattr(
        selfupdate,
        "wait_healthy",
        lambda port, proc, host: calls.append(f"healthy:{host}") or starts["n"] >= 2,
    )
    monkeypatch.setattr(selfupdate, "record_event", lambda *a, **k: None)

    def fake_revert(repo, sha):
        calls.append("revert")
        return True

    monkeypatch.setattr(selfupdate, "revert_merge", fake_revert)

    rc = selfupdate.main(
        [
            "--port", "8000",
            "--host", "192.168.1.10",
            "--repo", str(tmp_path),
            "--db", str(tmp_path / "x.db"),
            "--story", "S-1",
            "--merge-sha", "deadbee",
            "--backend-dir", str(tmp_path),
        ]
    )
    assert rc == 0
    assert calls == ["start:192.168.1.10", "healthy:192.168.1.10",
                     "revert", "start:192.168.1.10", "healthy:192.168.1.10"]
