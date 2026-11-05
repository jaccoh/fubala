"""Process-group bookkeeping for every subprocess Fubala spawns.

Incident: six `sh tools/test.sh` -> `pytest -n 4` trees survived a story
cancel and burned ~4.5 cores for hours. ``subprocess.run(..., timeout=)`` kills
the direct child only; the grandchildren (pytest's xdist workers, npm's child
processes) keep running because nothing ever signalled their group.

Fix: every spawn site puts its child in a NEW session (own process group) and
marks the child env (``FUBALA_PROC_GROUP`` + ``FUBALA_STORY_ID``) so the group
can be recognised later by scanning ``/proc``. Groups are registered per story
id, and every stop path kills the WHOLE group:

* the spawn site's own timeout (SIGTERM, then SIGKILL after a grace period),
* ``StoryRunner.cancel`` (story stopped mid-run),
* ``StoryRunner.stop`` (server shutdown),
* the periodic / startup sweep — the safety net for a server that died without
  running any of the above (a SIGKILLed server leaves no in-memory registry,
  but the marked processes are still visible in ``/proc``).

The registry is in-memory and guarded by ONE module lock. Spawn sites must hold
that lock across Popen+register, and the sweep holds it across scan+kill-list,
so a freshly spawned group is never swept between its first appearance in
``/proc`` and its registration.

Escapees: a ``setsid nohup cmd &`` started by a run leaves the run's process
group, so no group kill reaches it. Every spawn also gets a ``FUBALA_RUN_ID``
(uuid) in its env, inherited by escapees; when the run ends (normally, by
timeout or by cancel) ``reap_run`` kills everything still carrying that id, or
still in the run's group, and reports each kill (``report``: a story event with
command, pid, age and reason). ``reap_story`` does the same for a whole story
on cancel. Known gap: an escapee that runs ``env -i`` loses its markers and is
invisible to the reapers and the sweep (worker pods have no such gap: the
pod-side reaper works from the process table alone, see ``pods.REAP_SCRIPT``).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

# env markers carried by every tracked child (and inherited by its children)
MARKER = "FUBALA_PROC_GROUP"
STORY_ID_VAR = "FUBALA_STORY_ID"
# pid of the process that spawned the group: a LIVE spawner still owns its
# groups (another Fubala instance — or another xdist worker running this very
# suite); a DEAD spawner means nobody is left waiting, i.e. a true orphan.
# Deliberately NOT a boot id: a boot id of "not me" would also spare the
# orphans a dead server left behind, which is the sweep's entire purpose.
SPAWNER_PID_VAR = "FUBALA_SPAWNER_PID"
# per-spawn uuid: scopes cleanup to ONE run (the story id alone would also
# kill a concurrent run of the same story); inherited by setsid escapees
RUN_ID_VAR = "FUBALA_RUN_ID"

# SIGTERM first (children get to flush/teardown), SIGKILL after this grace
GRACE_SECONDS = 10.0
# periodic sweep cadence (seconds); env-overridable for tests
SWEEP_INTERVAL = 600.0
SWEEP_INTERVAL_VAR = "FUBALA_PROC_SWEEP_INTERVAL"

_LOCK = threading.Lock()
# story id -> set of process-group ids currently spawned for it
_REGISTRY: dict[str, set[int]] = {}
# run ids of spawns whose spawn site has not reaped them yet
_ACTIVE_RUNS: set[str] = set()

_HAS_PROC = Path("/proc").is_dir()


def mark_env(
    env: dict[str, str] | None, story_id: str | None, run_id: str | None = None
) -> dict[str, str] | None:
    """Return a child env marked as a Fubala-tracked process group member.

    ``env=None`` means "inherit the parent env"; when a story id is supplied a
    full copy of ``os.environ`` is marked so Popen still gets a complete env.

    With no story id the markers are REMOVED from the result. Inheritance
    would otherwise leak them: when this Fubala (or a test suite) runs inside
    another Fubala's verify tree, ``os.environ`` already carries them, and an
    unattributable child must stay invisible to the sweep — it has no registry
    entry, so a marked-but-unregistered group would be killed mid-run. The
    markers only ever reach CHILD envs — writing them into ``os.environ``
    would make the server itself sweepable.
    """
    if story_id:
        child = dict(env) if env is not None else dict(os.environ)
        child[MARKER] = "1"
        child[STORY_ID_VAR] = str(story_id)
        child[SPAWNER_PID_VAR] = str(os.getpid())
        if run_id:
            child[RUN_ID_VAR] = run_id
        return child
    if env is None:
        if not any(
            v in os.environ for v in (MARKER, STORY_ID_VAR, SPAWNER_PID_VAR, RUN_ID_VAR)
        ):
            return None  # clean parent env: plain inheritance is correct
        env = dict(os.environ)  # type: ignore[assignment]
    child = dict(env)
    child.pop(MARKER, None)
    child.pop(STORY_ID_VAR, None)
    child.pop(SPAWNER_PID_VAR, None)
    child.pop(RUN_ID_VAR, None)
    return child


def register(story_id: str | None, pgid: int) -> None:
    """Record that `pgid` belongs to `story_id` (idempotent)."""
    if not story_id or pgid is None:
        return
    with _LOCK:
        _REGISTRY.setdefault(story_id, set()).add(pgid)


def unregister(story_id: str | None, pgid: int) -> None:
    """Drop `pgid` from `story_id`'s registered groups (idempotent)."""
    if not story_id or pgid is None:
        return
    with _LOCK:
        groups = _REGISTRY.get(story_id)
        if not groups:
            return
        groups.discard(pgid)
        if not groups:
            _REGISTRY.pop(story_id, None)


def registered_groups(story_id: str) -> set[int]:
    """A copy of the groups currently registered for `story_id`."""
    with _LOCK:
        return set(_REGISTRY.get(story_id, ()))


def popen(
    argv: list[str] | tuple[str, ...],
    *,
    story_id: str | None = None,
    env: dict[str, str] | None = None,
    **kwargs: object,
) -> tuple[subprocess.Popen, int | None]:
    """Popen in its OWN process group, registered before the lock is released.

    ``start_new_session=True`` is forced (never overridden by ``kwargs``): the
    whole child tree — shells, pytest's xdist workers, npm's helpers — shares
    one process group id we can signal. The env is marked when a story id is
    given, and the Popen+register pair runs under the module lock so a
    concurrent sweep can never observe a marked-but-unregistered group.

    The child carries a fresh ``run_id`` (also an attribute of the returned
    Popen: ``child.fubala_run_id``, None without a story id) for ``reap_run``.

    Returns ``(child, pgid)``. The pgid is ALWAYS real (never None) — the
    spawn site needs it to kill the group on its own timeout even when the
    child is not attributable to a story — but it is registered only when a
    story id was supplied; unregistering an unregistered pgid is a no-op.
    """
    run_id = uuid.uuid4().hex if story_id else None
    marked = mark_env(env, story_id, run_id)
    if marked is not None:
        kwargs["env"] = marked
    kwargs["start_new_session"] = True
    with _LOCK:
        child = subprocess.Popen(argv, **kwargs)  # noqa: S603 - argv is ours
        try:
            pgid = os.getpgid(child.pid)
        except (ProcessLookupError, OSError):
            # died before we could ask: start_new_session makes pgid == pid
            pgid = child.pid
        child.fubala_run_id = run_id  # type: ignore[attr-defined]
        if story_id:
            _REGISTRY.setdefault(story_id, set()).add(pgid)
            _ACTIVE_RUNS.add(run_id)  # type: ignore[arg-type]
    return child, pgid


# ---- liveness ---------------------------------------------------------------


def _iter_proc_stat() -> list[tuple[int, str, int]]:
    """(pid, state, pgid) for every process visible in /proc (zombies too)."""
    out: list[tuple[int, str, int]] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            raw = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue  # gone, or not ours to read
        # comm (field 2) is parenthesised and may contain spaces/parens
        rest = raw[raw.rfind(")") + 1 :].split()
        if len(rest) < 3:
            continue
        try:
            out.append((int(entry), rest[0], int(rest[2])))
        except ValueError:
            continue
    return out


def group_alive(pgid: int | None) -> bool:
    """True when a live (non-zombie) process still belongs to `pgid`."""
    if not pgid:
        return False
    if _HAS_PROC:
        # zombies stay in their group until reaped; a reaped-away group must
        # not look alive because pid 1 is slow to collect an orphan
        return any(
            pgrp == pgid and state != "Z" for _pid, state, pgrp in _iter_proc_stat()
        )
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    return True


def _pid_alive(pid: int) -> bool:
    """True when `pid` is a live process (zombies don't own groups)."""
    if pid <= 0:
        return False
    if _HAS_PROC:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return False
        rest = raw[raw.rfind(")") + 1 :].split()
        return bool(rest) and rest[0] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---- the kill primitive -----------------------------------------------------


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError) as e:
        log.debug("signal %s to group %d not delivered: %s", sig, pgid, e)


def kill_group(
    pgid: int | None,
    *,
    grace: float = GRACE_SECONDS,
    wait: bool = True,
    proc: subprocess.Popen | None = None,
) -> bool:
    """Kill every process in `pgid`: SIGTERM, escalate to SIGKILL.

    `wait=True` (the timeout paths) blocks until the group is dead or the
    grace expires, then SIGKILLs whatever is left; `wait=False` (async stop
    paths) returns immediately with the SIGKILL escalation armed on a timer so
    a SIGTERM-ignoring grandchild still cannot outlive the grace by much.
    `proc` is reaped when given — it is our direct child, so only we can.
    """
    if not pgid:
        if proc is not None:
            _reap(proc)
        return False

    def escalate() -> None:
        if group_alive(pgid):
            log.warning(
                "process group %d ignored SIGTERM for %.0fs — SIGKILL", pgid, grace
            )
            _signal_group(pgid, signal.SIGKILL)

    timer = threading.Timer(grace, escalate)
    timer.daemon = True
    timer.start()
    _signal_group(pgid, signal.SIGTERM)

    dead = not group_alive(pgid)
    if wait and not dead:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not group_alive(pgid):
                dead = True
                break
            time.sleep(0.05)
        if not dead:
            log.warning("process group %d outlived its %.0fs grace — SIGKILL", pgid, grace)
            _signal_group(pgid, signal.SIGKILL)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if not group_alive(pgid):
                    break
                time.sleep(0.05)
        dead = not group_alive(pgid)
    if dead:
        timer.cancel()
    if proc is not None:
        _reap(proc)
    return dead


def _reap(proc: subprocess.Popen, timeout: float = GRACE_SECONDS) -> None:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("child %d could not be reaped after %ss", proc.pid, timeout)
    except Exception as e:  # noqa: BLE001 - reaping must never raise
        log.debug("reaping child %d failed: %s", proc.pid, e)


# ---- reporting + reaping leftovers ---------------------------------------------
# A run that ends leaves nothing behind. The reporter (registered by
# StoryRunner.start) turns every kill into a story event.

# SIGTERM -> SIGKILL grace of the end-of-run reapers (bounded: reaping must
# never hold up finalisation)
REAP_GRACE = 2.0
CMD_CLIP = 200

_REPORTER: Callable[[str, str, list[dict[str, object]]], None] | None = None
# (pid, start ticks) already reported by a cancel: the run's own end-of-run
# reap then terminates them again but does not report them twice
_REPORTED: set[tuple[int, object]] = set()


def set_reporter(fn: Callable[[str, str, list[dict[str, object]]], None] | None) -> None:
    """Register (or clear, with None) ``fn(story_id, reason, entries)``."""
    global _REPORTER
    _REPORTER = fn


def report(story_id: str | None, reason: str, entries: list[dict[str, object]]) -> None:
    """Hand killed processes to the reporter. ``entries`` items are
    ``{command, pid, age_s}``; ``reason`` is round_end, timeout, cancel or
    sweep. A reporter failure is logged, never raised."""
    fn = _REPORTER
    if fn is None or not story_id or not entries:
        return
    try:
        fn(story_id, reason, entries)
    except Exception:  # noqa: BLE001 - reporting must never break a stop path
        log.exception("proc cleanup reporter failed")


try:
    _CLK_TCK = float(os.sysconf("SC_CLK_TCK"))
except (AttributeError, ValueError, OSError):  # pragma: no cover - non-POSIX
    _CLK_TCK = 100.0


def _uptime() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _proc_info(pid: int) -> dict[str, object] | None:
    """pid, comm, state, pgid, starttime (clock ticks) and uid of a process."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        uid = os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None
    lo, hi = raw.find("("), raw.rfind(")")
    rest = raw[hi + 1 :].split()
    if lo < 0 or hi < lo or len(rest) < 20:
        return None
    try:
        return {
            "pid": pid,
            "comm": raw[lo + 1 : hi],
            "state": rest[0],
            "pgid": int(rest[2]),
            "start": int(rest[19]),
            "uid": uid,
        }
    except ValueError:
        return None


def _entry(info: dict[str, object], uptime: float) -> dict[str, object]:
    pid = int(info["pid"])  # type: ignore[call-overload]
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        raw = b""
    cmd = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip() or f"[{info['comm']}]"
    age = max(0, int(uptime - int(info["start"]) / _CLK_TCK))  # type: ignore[call-overload]
    return {"command": cmd[:CMD_CLIP], "pid": pid, "age_s": age, "start": info["start"]}


def _same_alive(pid: int, start: object) -> bool:
    """`pid` is still the same live (non-zombie) process (guards pid reuse)."""
    info = _proc_info(pid)
    return info is not None and info["state"] not in ("Z", "X") and info["start"] == start


def _scan(match: Callable[[dict[str, object]], bool]) -> list[dict[str, object]]:
    """Live, non-zombie processes of our uid (never ourselves) accepted by
    `match`, as report entries (plus their start ticks and pgid)."""
    me, uid, up = os.getpid(), os.getuid(), _uptime()
    found: list[dict[str, object]] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        info = _proc_info(int(entry))
        if info is None or info["state"] in ("Z", "X") or info["uid"] != uid:
            continue
        if match(info):
            found.append({**_entry(info, up), "pgid": info["pgid"]})
    return found


def _terminate(targets: list[dict[str, object]], grace: float, wait: bool) -> None:
    """SIGTERM each target, then SIGKILL those still the same process after
    `grace` (inline when `wait`, on a daemon timer otherwise)."""
    for t in targets:
        try:
            os.kill(int(t["pid"]), signal.SIGTERM)  # type: ignore[call-overload]
        except (ProcessLookupError, PermissionError):
            pass

    def escalate() -> None:
        for t in targets:
            if _same_alive(int(t["pid"]), t["start"]):  # type: ignore[call-overload]
                log.warning("pid %s ignored SIGTERM — SIGKILL", t["pid"])
                try:
                    os.kill(int(t["pid"]), signal.SIGKILL)  # type: ignore[call-overload]
                except (ProcessLookupError, PermissionError):
                    pass

    if wait:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not any(_same_alive(int(t["pid"]), t["start"]) for t in targets):  # type: ignore[call-overload]
                return
            time.sleep(0.05)
        escalate()
    else:
        timer = threading.Timer(grace, escalate)
        timer.daemon = True
        timer.start()


def _public(targets: list[dict[str, object]]) -> list[dict[str, object]]:
    return [{k: t[k] for k in ("command", "pid", "age_s")} for t in targets]


def reap_run(
    story_id: str | None,
    run_id: str | None,
    pgid: int | None,
    *,
    reason: str = "round_end",
) -> list[dict[str, object]]:
    """Kill everything one finished spawn left behind and report it.

    Matches live same-uid processes by environ ``FUBALA_RUN_ID`` (setsid/nohup
    escapees inherit it) or by membership of `pgid`; a pgid member carrying
    ANOTHER run's id is left alone. Call it after the child has been waited.
    SIGTERM, ~2s, SIGKILL. Bounded, never raises; returns the killed entries.
    """
    try:
        if not _HAS_PROC:
            return []

        def match(info: dict[str, object]) -> bool:
            env_id = _environ_of(int(info["pid"])).get(RUN_ID_VAR)  # type: ignore[call-overload]
            if run_id and env_id == run_id:
                return True
            return bool(pgid) and info["pgid"] == pgid and (not env_id or env_id == run_id)

        with _LOCK:
            targets = _scan(match)
        if targets:
            if pgid and any(t["pgid"] == pgid for t in targets):
                kill_group(pgid, grace=REAP_GRACE, wait=True)
            rest = [t for t in targets if t["pgid"] != pgid]
            if rest:
                _terminate(rest, REAP_GRACE, wait=True)
            fresh = [t for t in targets if (t["pid"], t["start"]) not in _REPORTED]
            _REPORTED.difference_update((t["pid"], t["start"]) for t in targets)
            report(story_id, reason, _public(fresh))
        return _public(targets)
    except Exception:  # noqa: BLE001 - reaping must never raise into a spawn site
        log.exception("reaping run %s failed", run_id)
        return []
    finally:
        if run_id:
            with _LOCK:
                _ACTIVE_RUNS.discard(run_id)


def reap_story(story_id: str, *, reason: str = "cancel") -> list[dict[str, object]]:
    """SIGTERM (timed SIGKILL, non-blocking) every live process this server
    spawned for `story_id`, escapees included, and report them."""
    try:
        if not _HAS_PROC:
            return []
        me = str(os.getpid())

        def match(info: dict[str, object]) -> bool:
            env = _environ_of(int(info["pid"]))  # type: ignore[call-overload]
            return env.get(STORY_ID_VAR) == story_id and env.get(SPAWNER_PID_VAR) == me

        with _LOCK:
            targets = _scan(match)
        if targets:
            _terminate(targets, GRACE_SECONDS, wait=False)
            if len(_REPORTED) > 1000:
                _REPORTED.clear()
            _REPORTED.update((t["pid"], t["start"]) for t in targets)
            report(story_id, reason, _public(targets))
        return _public(targets)
    except Exception:  # noqa: BLE001 - cancel must never fail on cleanup
        log.exception("reaping story %s failed", story_id)
        return []


# ---- pod hooks ---------------------------------------------------------------
# kill_story/kill_all also reach the story's worker pod. The hooks are
# registered by app.pods, so this module never imports the kubernetes client.

_POD_KILL_STORY: Callable[[str], None] | None = None
_POD_KILL_ALL: Callable[[], None] | None = None


def set_pod_hooks(
    kill_story_hook: Callable[[str], None] | None,
    kill_all_hook: Callable[[], None] | None,
) -> None:
    """Register (or clear, with None) the pod deletion hooks."""
    global _POD_KILL_STORY, _POD_KILL_ALL
    _POD_KILL_STORY = kill_story_hook
    _POD_KILL_ALL = kill_all_hook


def _call_hook(hook: Callable[..., None] | None, *args: str) -> None:
    if hook is None:
        return
    try:
        hook(*args)
    except Exception:  # noqa: BLE001 - a pod hook must never break a stop path
        log.exception("worker pod kill hook failed")


def kill_story(story_id: str) -> int:
    """SIGTERM (+ timed SIGKILL escalation) every group spawned for `story_id`.

    Non-blocking on purpose: the caller is usually an async stop path that
    just needs the subprocess tree to start dying so the blocked worker thread
    watching it can observe death and finish.
    """
    with _LOCK:
        pgids = set(_REGISTRY.get(story_id, ()))
    # escapees (setsid/nohup) are in no registered group: find them by marker
    # first, while everything is still alive to be named in the report
    reap_story(story_id)
    for pgid in pgids:
        log.info("killing process group %d for story %s", pgid, story_id)
        kill_group(pgid, wait=False)
    _call_hook(_POD_KILL_STORY, story_id)
    return len(pgids)


def kill_all() -> int:
    """Same as kill_story, for every registered group (shutdown path)."""
    with _LOCK:
        pgids = {pgid for groups in _REGISTRY.values() for pgid in groups}
    for pgid in pgids:
        log.info("killing process group %d (shutdown)", pgid)
        kill_group(pgid, wait=False)
    _call_hook(_POD_KILL_ALL)
    return len(pgids)


# ---- executors ----------------------------------------------------------------


class Executor(Protocol):
    """Where a trusted verify/test command runs: this process tree or a pod."""

    node_name: str | None
    is_pod: bool

    def prepare(self) -> None: ...

    def run(
        self,
        command: str,
        cwd_rel: str,
        env: dict[str, str] | None,
        timeout: float,
        on_output: Callable[[str], None] | None = None,
    ) -> tuple[int, str]: ...

    def kill(self) -> None: ...

    def close(self) -> None: ...


class LocalExecutor:
    """The in-process path: a registered process group under `cwd`.

    rc 124 on timeout (whole group killed), rc 125 when the shell could not be
    spawned; output is stdout plus a `stderr:` section when stderr has text.
    """

    node_name: str | None = None
    is_pod = False

    def __init__(self, cwd: Path | str, story_id: str | None = None) -> None:
        self.cwd = Path(cwd)
        self.story_id = story_id

    def prepare(self) -> None:
        return None

    def run(
        self,
        command: str,
        cwd_rel: str,
        env: dict[str, str] | None,
        timeout: float,
        on_output: Callable[[str], None] | None = None,
    ) -> tuple[int, str]:
        cwd = self.cwd / cwd_rel if cwd_rel else self.cwd
        try:
            # own session + Popen+register under the module lock: a plain
            # subprocess.run(timeout=) kills only the direct child, orphaning
            # grandchildren (xdist workers), and an unregistered marked group
            # would be swept mid-run. Unregistered (no story id) is still
            # timeout-killable.
            child, pgid = popen(
                ["/bin/sh", "-c", command],
                story_id=self.story_id,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        except OSError as e:
            return 125, f"verify could not run: {e}"
        reason = "round_end"
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # kill the WHOLE group, not just the shell: graceful (SIGTERM,
            # SIGKILL after the grace) so pytest can flush its reports
            reason = "timeout"
            kill_group(pgid, wait=True, proc=child)
            return 124, f"verify timed out after {timeout}s"
        finally:
            # whatever the command started and left running (setsid escapees)
            reap_run(
                self.story_id, getattr(child, "fubala_run_id", None), pgid, reason=reason
            )
            if self.story_id:
                unregister(self.story_id, pgid)
        output = stdout or ""
        if (stderr or "").strip():
            output += f"\nstderr:\n{stderr}"
        return child.returncode, output

    def kill(self) -> None:
        if self.story_id:
            kill_story(self.story_id)

    def close(self) -> None:
        return None


# ---- the sweep --------------------------------------------------------------


def _environ_of(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for chunk in raw.split(b"\0"):
        if b"=" not in chunk:
            continue
        key, _, value = chunk.partition(b"=")
        try:
            out[key.decode()] = value.decode()
        except UnicodeDecodeError:
            continue
    return out


def _marked_groups() -> dict[int, tuple[int, str, int | None, str]]:
    """pgid -> (pid, story id, spawner pid, run id) for every live marked process.

    Only finds processes whose /proc/<pid>/environ we may read (our own
    children). Never keys off a bare pid: a pgid is killed only because a
    MARKED process was found in it. The spawner pid decides ownership: a LIVE
    spawner (this process, or another Fubala — including an xdist worker
    running this very suite) still owns its groups; a DEAD one means the group
    is orphaned and nothing will ever wait on it again. This is what makes the
    sweep the safety net for a server that died hard (SIGKILL/OOM): the
    markers live in the CHILDREN's env, not in this process's memory, so a
    rebooting server still finds — and kills — its predecessor's orphans.
    """
    found: dict[int, tuple[int, str, int | None, str]] = {}
    my_pid = os.getpid()
    my_pgid = os.getpgrp()
    for pid, state, pgid in _iter_proc_stat():
        if state == "Z" or pid == my_pid or pgid in (my_pid, my_pgid):
            continue
        env = _environ_of(pid)
        if env.get(MARKER) != "1":
            continue
        # a marked process outside our own groups: /proc resolution only
        try:
            spawner = int(env.get(SPAWNER_PID_VAR, "") or 0) or None
        except ValueError:
            spawner = None
        found.setdefault(
            pgid, (pid, env.get(STORY_ID_VAR, ""), spawner, env.get(RUN_ID_VAR, ""))
        )
    return found


def sweep(running_story_ids: set[str] | None = None) -> list[int]:
    """Kill marked groups that no running story owns; return the killed pgids.

    The scan AND the kill-list are built under the module lock: a group spawned
    a microsecond ago is already registered by then (``popen`` holds the same
    lock across Popen+register), so it can never be swept before registration.
    Spared: any group REGISTERED in this process, or carrying an ACTIVE run id
    (we are still waiting on it — the spawn site reaps and unregisters only
    when its run ends; an escapee of a FINISHED run of this server is a
    leftover and is killed, reason ``sweep``), groups whose SPAWNER is another
    live process (another Fubala owns them — a second board on the same host,
    or an xdist worker running this very suite), and the server's own
    pid/pgid.

    Killed by design: a marked group whose spawner is gone (a server that died
    hard — SIGKILL/OOM — leaves exactly that) or whose story stopped running
    here. That is the sweep's entire purpose: nothing distinguishes "orphan
    from a dead server" from "orphan nobody waits on", and both must die.
    """
    if not _HAS_PROC:
        return []
    running = set(running_story_ids or ())
    with _LOCK:
        spared = {
            pgid
            for story_id in running
            for pgid in _REGISTRY.get(story_id, ())
        }
        # EVERY registered group is spared, not just the running stories': a
        # registered group is one this process spawned and has not reaped yet
        # (the verify stage registers before waiting; the story can already be
        # out of `running` while its worker thread is still parked on the child)
        for groups in _REGISTRY.values():
            spared.update(groups)
        marked = _marked_groups()
        # registry entries for stories that stopped running are dropped, not
        # signalled blindly: a pgid only enters the kill list when a MARKED,
        # still-live process was found inside it via /proc
        for story_id in [s for s in _REGISTRY if s not in running]:
            dead = {p for p in _REGISTRY.get(story_id, ()) if p not in marked}
            _REGISTRY[story_id] -= dead
            if not _REGISTRY[story_id]:
                _REGISTRY.pop(story_id, None)
        kill_list: list[tuple[int, str, int]] = []
        my_pid = os.getpid()
        for pgid, (pid, story_id, spawner, run_id) in marked.items():
            if pgid in spared or (run_id and run_id in _ACTIVE_RUNS):
                continue
            if spawner is not None and spawner != my_pid and _pid_alive(spawner):
                # someone else's live subprocess tree: not an orphan. OUR OWN
                # groups are spared only while active (registered pgid or run
                # id above): a finished run's escapee is a leftover.
                continue
            # marked AND unregistered (or registered to a story that is no
            # longer running) = nobody is waiting on this group any more
            kill_list.append((pgid, story_id or "(unknown)", pid))
    killed: list[int] = []
    seen: set[int] = set()
    reports: dict[str, list[dict[str, object]]] = {}
    up = _uptime()
    for pgid, story_id, pid in kill_list:
        if pgid in seen:
            continue
        seen.add(pgid)
        log.warning(
            "proc sweep: killing orphaned process group %d (story %s)", pgid, story_id
        )
        info = _proc_info(pid)
        if info is not None and story_id != "(unknown)":
            reports.setdefault(story_id, []).append(_public([_entry(info, up)])[0])
        kill_group(pgid, wait=False)
        killed.append(pgid)
    for story_id, entries in reports.items():
        report(story_id, "sweep", entries)
    return killed


def sweep_interval() -> float:
    """Seconds between proc sweeps; FUBALA_PROC_SWEEP_INTERVAL overrides."""
    raw = os.environ.get(SWEEP_INTERVAL_VAR, "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            log.warning("ignoring unparsable %s=%r", SWEEP_INTERVAL_VAR, raw)
    return SWEEP_INTERVAL
