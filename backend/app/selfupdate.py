"""Self-update helper: spawned detached after a story merge.

Waits for the old server to release the port, starts the new code, and
health-checks it. If the new server won't come up healthy, reverts the merge
and starts again — a fubala that breaks itself must be able to un-break
itself. Deliberately dependency-light (stdlib only): this is the recovery
path, so it must not import the app it may need to roll back.

Usage:
    python -m app.selfupdate --port 8000 --host 127.0.0.1 --repo /path \
        --db /path/fubala.db --story <id> --merge-sha <sha> \
        --backend-dir /path/to/backend [--old-pid <pid>]

`--host`/`--port` are required: they carry the bind (from FUBALA_HOST/
FUBALA_PORT) that the new server must come back on, and drive the
port-free/health probing. The server itself is started via the app's own
entry point (`python -m app`) so its Settings decide the bind — see
_server_command.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

LOG_PATH = Path(os.environ.get("FUBALA_SELFUPDATE_LOG", "/tmp/fubala-selfupdate.log"))
HEALTH_PATH = "/api/v1/health"
START_TIMEOUT_S = 60
PORT_FREE_TIMEOUT_S = 60
# NOT a spawn-affecting default: --host is required on the CLI. This constant
# only gives the port-free/health probes a sane host when called without one.
DEFAULT_HOST = "127.0.0.1"
# how long to wait for the old server to exit on its own before signalling it,
# and how long each signal gets to work — injectable per call for fast tests
OLD_PID_GRACE_S = 3.0
OLD_PID_TIMEOUT_S = 15.0


def log(message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    print(line, flush=True)
    try:
        with LOG_PATH.open("a") as fh:
            fh.write(line + "\n")
    except OSError as e:
        # the log file is a convenience; losing it must not kill the updater
        print(f"selfupdate: could not append to {LOG_PATH}: {e}", file=sys.stderr)


def _probe_host(host: str) -> str:
    """A literal address to dial for bind-host `host`. 0.0.0.0 / :: are
    wildcards you cannot connect TO — loopback is what actually answers."""
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host


def _port_in_use(port: int, host: str = DEFAULT_HOST) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((_probe_host(host), port)) == 0


def _healthy(port: int, host: str = DEFAULT_HOST) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://{_probe_host(host)}:{port}{HEALTH_PATH}", timeout=3
        ) as res:
            return res.status == 200
    except Exception:
        return False


def wait_port_free(port: int, host: str = DEFAULT_HOST) -> bool:
    deadline = time.monotonic() + PORT_FREE_TIMEOUT_S
    while time.monotonic() < deadline:
        if not _port_in_use(port, host):
            return True
        time.sleep(0.5)
    return False


def _is_stranger(pid: int) -> bool:
    """Best-effort (Linux-only) guard against PID reuse: True only when we can
    *prove* the pid now runs something that is not a fubala server. Unknown
    (no /proc, unreadable) counts as ours — better to signal one wrong process
    once than to leave a fubala that is mid-update down."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return b"uvicorn" not in fh.read()
    except OSError:
        return False


def ensure_exited(
    pid: int, grace_s: float = OLD_PID_GRACE_S, timeout_s: float = OLD_PID_TIMEOUT_S
) -> bool:
    """Wait until the old server process is fully dead before the replacement
    starts. The spawner SIGTERMs itself a moment after spawning this helper,
    so the process usually dies on its own; after the grace period we escalate
    to SIGTERM/SIGKILL, aimed at its process group when it leads one and at
    the pid itself when it does not. A dead or absent pid is success.

    Returns True once the pid is gone, False if it survived everything.
    """
    def alive(p: int) -> bool:
        if p <= 1:
            return False  # 0/-1 are os.kill wildcards, never a server pid
        try:
            os.kill(p, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, owned by someone else
        return True

    if not alive(pid):
        return True

    time.sleep(grace_s)  # let the spawner's own SIGTERM land first
    if not alive(pid):
        return True

    if _is_stranger(pid):
        # the pid was recycled by an unrelated program — the old server must
        # already be gone, and the newcomer must not be touched
        log(f"pid {pid} was recycled by another program; treating as exited")
        return True

    for sig in (signal.SIGTERM, signal.SIGKILL):
        for signal_it in (
            lambda: os.killpg(pid, sig),  # primary: server runs in its own group
            lambda: os.kill(pid, sig),  # fallback: pid may not lead its group
        ):
            try:
                signal_it()
            except OSError:
                pass
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not alive(pid):
                return True
            time.sleep(0.1)
    return not alive(pid)


def _server_command(
    backend_dir: str, port: int, db_path: str, host: str = DEFAULT_HOST
) -> tuple[list[str], dict]:
    """An interpreter that can import uvicorn: ours if it has it, else uv's.

    The server is launched through the app's own entry point (`-m app`), so
    the entry point's Settings decide the bind — the same single source as
    the `python -m app` start command. host/port travel as FUBALA_HOST/
    FUBALA_PORT in the env instead of CLI flags.

    Rollback safety: a rolled-back (pre-merge) tree may not have
    app/__main__.py, in which case `-m app` would fail outright. There we
    fall back to the explicit uvicorn argv with the same host/port values.
    """
    import shutil

    has_entry_point = (Path(backend_dir) / "app" / "__main__.py").is_file()

    # each candidate is (probe argv, uvicorn argv, entry-point argv)
    ours = [sys.executable]
    candidates = [
        (ours + ["-c", "import uvicorn"],
         ours + ["-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port)],
         ours + ["-m", "app"]),
    ]
    if shutil.which("uv"):
        uv_prefix = ["uv", "run", "--project", backend_dir, "python"]
        candidates.append(
            (uv_prefix + ["-c", "import uvicorn"],
             uv_prefix + ["-m", "uvicorn", "app.main:app", "--host", host,
                          "--port", str(port)],
             uv_prefix + ["-m", "app"]),
        )

    for probe, uvicorn_argv, module_argv in candidates:
        try:
            if subprocess.run(
                probe, cwd=backend_dir, capture_output=True, timeout=60
            ).returncode == 0:
                if has_entry_point:
                    # the app's own entry point: its Settings decide the bind
                    env = dict(os.environ)
                    env["FUBALA_HOST"] = host
                    env["FUBALA_PORT"] = str(port)
                    return module_argv, env
                # pre-merge tree without app/__main__.py: `-m app` would fail
                return uvicorn_argv, dict(os.environ)
        except (OSError, subprocess.TimeoutExpired):
            continue
    # last resort: assume our interpreter anyway
    if has_entry_point:
        env = dict(os.environ)
        env["FUBALA_HOST"] = host
        env["FUBALA_PORT"] = str(port)
        return candidates[0][2], env
    return candidates[0][1], dict(os.environ)


def start_server(
    backend_dir: str, port: int, db_path: str, host: str = DEFAULT_HOST
) -> subprocess.Popen:
    argv, base_env = _server_command(backend_dir, port, db_path, host)
    env = dict(base_env)
    env["FUBALA_DATABASE_PATH"] = db_path
    log(f"starting server: {' '.join(argv)}")
    return subprocess.Popen(
        argv,
        cwd=backend_dir,
        env=env,
        stdout=open(LOG_PATH.with_suffix(".server.log"), "ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def record_event(db_path: str, story_id: str, kind: str, data: dict) -> None:
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute(
            "INSERT INTO story_events (story_id, kind, data, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                story_id,
                kind,
                json.dumps(data),
                datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" "),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:  # best-effort bookkeeping
        log(f"could not record event: {e}")


def wait_healthy(port: int, proc: subprocess.Popen, host: str = DEFAULT_HOST) -> bool:
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log(f"server exited early with code {proc.returncode}")
            return False
        if _healthy(port, host):
            return True
        time.sleep(1.0)
    return False


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()


def merge_touched_frontend(repo: str, merge_sha: str, run=subprocess.run) -> bool:
    """True iff the merge's first-parent diff changed anything under
    `frontend/`.

    `-m --first-parent` mirrors the revert semantics (`git revert -m 1`): we
    only care what the merge brought relative to the mainline we were on. A
    missing repo, a bogus sha, or git itself failing must never take the
    build path — there is nothing we can rebuild, so say "untouched".
    """
    try:
        res = run(
            ["git", "-C", repo, "diff-tree", "--no-commit-id", "--name-only",
             "-r", "-m", "--first-parent", merge_sha],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"could not inspect merge {merge_sha}: {e}")
        return False
    if res.returncode != 0:
        log(f"diff-tree failed ({res.stderr.strip()[:300]})")
        return False
    return any(
        line.strip().startswith("frontend/") for line in res.stdout.splitlines()
    )


FRONTEND_BUILD_STEPS = (("npm", "ci"), ("npm", "run", "build"))
FRONTEND_BUILD_TIMEOUT_S = 600
DIST_BACKUP_NAME = ".dist.selfupdate-bak"


def build_frontend(backend_dir: str, run=subprocess.run) -> tuple[bool, str]:
    """Rebuild `frontend/dist` for a merge that touched `frontend/`.

    Runs strictly between the old server dying and the new one starting, so
    the dist swap is invisible to any client. `vite` can empty `dist` before
    failing, so `dist` is moved aside first and restored on any failure — a
    failed build must leave the old UI intact, not an empty one. A missing
    frontend sibling is a skip (True), never a reason to revert a merge.
    """
    frontend_dir = Path(backend_dir).parent / "frontend"
    if not frontend_dir.is_dir():
        log(f"no frontend dir at {frontend_dir}; skipping frontend build")
        return True, ""

    dist = frontend_dir / "dist"
    backup = frontend_dir / DIST_BACKUP_NAME

    def _restore_old_dist() -> None:
        if backup.exists():
            if dist.exists():
                shutil.rmtree(dist, ignore_errors=True)
            shutil.move(str(backup), str(dist))

    if dist.exists():
        try:
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            shutil.move(str(dist), str(backup))
        except OSError as e:
            log(f"could not move dist aside ({e}); building in place")

    output_tail = ""
    for step in FRONTEND_BUILD_STEPS:
        log(f"frontend build: {' '.join(step)}")
        try:
            res = run(
                list(step),
                cwd=str(frontend_dir),
                capture_output=True,
                text=True,
                timeout=FRONTEND_BUILD_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as e:
            output_tail = (
                f"{(e.stdout or '')[-2000:]}\n{(e.stderr or '')[-2000:]}"
            )
            _restore_old_dist()
            return False, f"frontend build failed: {output_tail[-2000:]}"
        except OSError as e:
            output_tail = str(e)
            _restore_old_dist()
            return False, f"frontend build failed: {output_tail[-2000:]}"
        if res.returncode != 0:
            combined = f"{res.stdout or ''}\n{res.stderr or ''}"
            output_tail = combined[-2000:]
            _restore_old_dist()
            return False, f"frontend build failed: {output_tail}"

    try:
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
    except OSError:
        pass
    log("frontend build succeeded")
    return True, ""


def revert_merge(repo: str, merge_sha: str) -> bool:
    res = subprocess.run(
        ["git", "-C", repo, "revert", "--no-edit", "-m", "1", merge_sha],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        log(f"git revert failed ({res.stderr.strip()[:300]}); trying reset")
        res = subprocess.run(
            ["git", "-C", repo, "reset", "--hard", f"{merge_sha}^"],
            capture_output=True,
            text=True,
        )
        return res.returncode == 0
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--old-pid", type=int, default=None)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--story", required=True)
    parser.add_argument("--merge-sha", required=True)
    parser.add_argument("--backend-dir", required=True)
    args = parser.parse_args(argv)

    log(f"self-update for story {args.story}: waiting for port {args.port} to free")
    if not wait_port_free(args.port, args.host):
        log("old server never released the port; aborting self-update")
        record_event(
            args.db, args.story, "self_update",
            {"applied": False, "error": "port never freed"},
        )
        return 1

    if args.old_pid is not None:
        if not ensure_exited(args.old_pid):
            log("old server process never exited; aborting self-update")
            record_event(
                args.db, args.story, "self_update",
                {"applied": False, "error": "old pid never exited"},
            )
            return 1

    build_error = ""
    if merge_touched_frontend(args.repo, args.merge_sha):
        log("merge touched frontend/; rebuilding dist before starting")
        ok, build_error = build_frontend(args.backend_dir)
        if not ok:
            log("frontend build failed — reverting the merge without starting it")
            if not revert_merge(args.repo, args.merge_sha):
                log("revert failed; leaving the server down for the operator")
                record_event(
                    args.db, args.story, "self_update",
                    {
                        "applied": False,
                        "reverted": False,
                        "merge": args.merge_sha,
                        "error": build_error,
                    },
                )
                return 1
            proc = start_server(args.backend_dir, args.port, args.db, args.host)
            if wait_healthy(args.port, proc, args.host):
                log("rolled back to pre-merge code — healthy again")
                record_event(
                    args.db, args.story, "self_update",
                    {
                        "applied": False,
                        "reverted": True,
                        "merge": args.merge_sha,
                        "error": build_error,
                    },
                )
                return 0
            log("rollback server also unhealthy; leaving it down for the operator")
            record_event(
                args.db, args.story, "self_update",
                {
                    "applied": False,
                    "reverted": True,
                    "merge": args.merge_sha,
                    "error": build_error,
                },
            )
            return 1

    proc = start_server(args.backend_dir, args.port, args.db, args.host)
    if wait_healthy(args.port, proc, args.host):
        log("new code is up and healthy — self-update applied")
        record_event(
            args.db, args.story, "self_update",
            {"applied": True, "merge": args.merge_sha},
        )
        return 0

    log("new code failed health check — reverting the merge")
    stop_server(proc)
    if not revert_merge(args.repo, args.merge_sha):
        log("revert failed; leaving the server down for the operator")
        record_event(
            args.db, args.story, "self_update",
            {"applied": False, "reverted": False, "error": "revert failed"},
        )
        return 1

    proc = start_server(args.backend_dir, args.port, args.db, args.host)
    if wait_healthy(args.port, proc, args.host):
        log("rolled back to pre-merge code — healthy again")
        record_event(
            args.db, args.story, "self_update",
            {"applied": False, "reverted": True, "merge": args.merge_sha},
        )
        return 0
    log("rollback server also unhealthy; leaving it down for the operator")
    record_event(
        args.db, args.story, "self_update",
        {"applied": False, "reverted": True, "error": "rollback unhealthy"},
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
