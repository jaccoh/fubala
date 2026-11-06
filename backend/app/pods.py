"""Per-run worker pods: the PodExecutor (worker pods stories 1c and 3).

With ``run_executor`` = ``verify`` (or ``pod``), every trusted verify/test
command of a story run executes in ONE short-lived pod instead of the backend
process tree. The pod holds no credential at all (no ServiceAccount token, no
envFrom, no secret volume): the workspace goes in as a git bundle over exec
stdin (``workspaces.snapshot_bundle``), the verdict comes back as exit code
and output.

With ``run_executor`` = ``pod`` each headless CODE round also runs the
``claude`` CLI in that pod (`PodExecutor.run_headless`): the token and the
prompt travel only in an exec stdin frame, the stream feeds the board as
in-process, and the round's commits come back as a git bundle that
fast-forwards the local worktree.

Every problem before a run starts falls back to the in-process path for the
rest of the run (``PodUnavailable``): the pod is a performance/isolation
feature, never a new way for a story to fail. A headless run lost after its
exec started (``PodRunLost``) is an infrastructure failure instead: the
round is never rerun locally on top of half-known state.

The kubernetes client is imported lazily and only in `KubeApi`; tests inject a
fake with the same five methods.
"""

from __future__ import annotations

import base64
import logging
import math
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from . import code_graph, headless, procs, workspaces
from .settings_store import DEFAULTS

log = logging.getLogger(__name__)

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
MANAGED_LABEL = "app.kubernetes.io/managed-by"
MANAGED_VALUE = "fubala"
DEFAULT_STORY_LABEL = "fubala.dev/story"
MANAGED_SELECTOR = f"{MANAGED_LABEL}={MANAGED_VALUE}"

WORKER_UID = 10003
REPO_DIR = "/work/repo"
HEARTBEAT = "/work/.heartbeat"
HEARTBEAT_MAX_AGE = 900  # the pod's PID 1 exits once the heartbeat is older
HEARTBEAT_EVERY = 300.0  # the runner loop touches live pods this often
PENDING_LIMIT = 120.0  # a pod still Pending after this is deleted, run goes local
EXEC_MARGIN = 30.0  # backend outer bound on one exec: timeout + margin
MAX_OUTPUT = 400_000  # characters kept per exec (the tail; callers keep 4000)
MAX_PROCESSES = 4
CLAUDE_DIR = "/work/.claude"  # per-round CLAUDE_CONFIG_DIR: /work/.claude/<n>
TRANSCRIPT_CAP = 50_000_000  # bytes of the session JSONL copied back
CLOSE_TRANSCRIPT_TIMEOUT = 15.0  # transcript copy bound on cancel/kill
BUNDLE_END = "END-FUBALA-BUNDLE"
SPILL_BYTES = 64_000  # a CLI prompt over this goes through a file, not argv

POD_ENV = {
    "HOME": "/cache/home",
    "npm_config_cache": "/cache/npm",
    "UV_CACHE_DIR": "/cache/uv",
    "UV_PYTHON_PREFERENCE": "only-managed",
    "PYTEST_XDIST_AUTO_NUM_WORKERS": str(MAX_PROCESSES),
}

# PID 1: exit (pod -> Succeeded) once the heartbeat is stale. Parameterised so
# the test can run the very same script with a short interval.
WATCHDOG_TEMPLATE = (
    'hb="{hb}"; [ -e "$hb" ] || touch "$hb"; '
    "while :; do "
    'now=$(date +%s); last=$(date -r "$hb" +%s 2>/dev/null || echo 0); '
    "[ $((now - last)) -gt {limit} ] && exit 0; "
    "sleep {interval}; done"
)


def watchdog_script(
    heartbeat: str = HEARTBEAT, limit: int = HEARTBEAT_MAX_AGE, interval: int = 30
) -> str:
    return WATCHDOG_TEMPLATE.format(hb=heartbeat, limit=int(limit), interval=int(interval))


_REAP_TEMPLATE = r"""
tck=$(getconf CLK_TCK 2>/dev/null) || tck=100
[ "$tck" -gt 0 ] 2>/dev/null || tck=100
up=0; { read -r up _ < @PROC@/uptime; } 2>/dev/null; up=${up%%.*}; : "${up:=0}"
me=$$; starts=""
for d in @PROC@/[0-9]*; do
  p=${d##*/}
  case "$p" in 1|"$me") continue;; esac
  line=""; { read -r line < "$d/stat"; } 2>/dev/null || continue
  comm=${line#*(}; comm=${comm%)*}
  set -- ${line##*) }
  [ "$#" -ge 20 ] || continue
  case "$1" in Z|X) continue;; esac
  [ "$4" = 1 ] && continue
  ca=""; { IFS= read -r ca < "$d/cmdline"; } 2>/dev/null
  case "$ca" in "touch @HB@"|"touch@HB@") continue;; esac
  start=${20}
  age=$((up - start / tck)); [ "$age" -ge 0 ] || age=0
  printf '%s\t%s\t%s\t%.200s\n' "$p" "$age" "$comm" "$ca"
  starts="$starts $p:$start"
done
[ -n "$starts" ] || exit 0
pids=""; for e in $starts; do pids="$pids ${e%%:*}"; done
@KILL@ -TERM $pids 2>/dev/null
sleep @GRACE@
surv=""
for e in $starts; do
  p=${e%%:*}; line=""
  { read -r line < @PROC@/$p/stat; } 2>/dev/null || continue
  set -- ${line##*) }
  [ "$#" -ge 20 ] || continue
  case "$1" in Z|X) continue;; esac
  [ "${20}" = "${e#*:}" ] && surv="$surv $p"
done
[ -z "$surv" ] || @KILL@ -KILL $surv 2>/dev/null
exit 0
"""


def reap_script(
    proc: str = "/proc", kill: str = "kill", heartbeat: str = HEARTBEAT, grace: int = 2
) -> str:
    """POSIX sh that kills every process left in the pod, setsid escapees
    included, and prints one line per process: ``pid<TAB>age_s<TAB>comm<TAB>argv``.

    Spared: pid 1, session-1 processes (the watchdog and its `sleep`), the
    script itself, zombies, and the concurrent `touch <heartbeat>`. The scan
    uses builtins only (no forked helper, so nothing it spawns shows up in
    it); `stat` is parsed after the LAST `)`. SIGTERM, `grace` seconds, then
    SIGKILL for the pids still alive with the same starttime (pid reuse).
    The shell's `read` drops the NULs of /proc/<pid>/cmdline, so argv arrives
    with its arguments run together; the comm column keeps it readable."""
    return (
        _REAP_TEMPLATE.replace("@PROC@", proc)
        .replace("@KILL@", kill)
        .replace("@HB@", heartbeat)
        .replace("@GRACE@", str(int(grace)))
    )


class PodUnavailable(Exception):
    """The pod path cannot serve this call; the caller runs in-process."""


class ExecTimeout(Exception):
    """An exec outlived its outer bound (command timeout + margin)."""


class FallbackCapFull(Exception):
    """The worker pod landed on the backend node while worker_fallback_parallel
    runs already load it. The pod is deleted; the runner requeues the story."""


class PodRunLost(Exception):
    """A headless run was lost after its exec started (transport, commit or
    pull-back failure). The local worktree is untouched; the runner fails the
    story as infrastructure and never reruns the round locally."""


class KubeApi(Protocol):
    namespace: str

    def create_pod(self, body: dict[str, Any]) -> None: ...

    def read_pod(self, name: str) -> dict[str, Any] | None:
        """{"phase": str, "node": str | None}, or None when the pod is gone."""
        ...

    def delete_pod(self, name: str) -> None: ...

    def list_pods(self, label_selector: str) -> list[dict[str, Any]]:
        """[{"name", "story": str | None, "node": str | None, "phase": str | None}]"""
        ...

    def exec(
        self,
        name: str,
        argv: list[str],
        stdin_bytes: bytes | None,
        on_output: Callable[[str], None] | None,
        timeout: float,
    ) -> tuple[int, str, str]:
        """Run argv in the pod; (returncode, stdout, stderr)."""
        ...


# ---- environment / preflight ----------------------------------------------------


def in_cluster() -> bool:
    return (SA_DIR / "token").is_file() and (SA_DIR / "namespace").is_file()


def worker_image_repo() -> str:
    """Registry path of the worker image, without tag (env, read per call)."""
    return os.environ.get("FUBALA_WORKER_IMAGE_REPO", "").strip()


def story_label() -> str:
    """Pod label key that maps a worker pod to its story id."""
    return os.environ.get("FUBALA_STORY_LABEL", "").strip() or DEFAULT_STORY_LABEL


def dev_node_label() -> str:
    """Node label key of the dev node (empty = no toleration or affinity)."""
    return os.environ.get("FUBALA_DEV_NODE_LABEL", "").strip()


def backend_node() -> str:
    return os.environ.get("NODE_NAME", "")


def preflight(worker_image: str) -> str | None:
    """Why the pod path cannot work here (None = it can)."""
    if not in_cluster():
        return "no in-cluster ServiceAccount token or namespace file"
    missing = [v for v in ("POD_NAME", "POD_UID", "NODE_NAME") if not os.environ.get(v)]
    if missing:
        return f"{', '.join(missing)} not set in the backend environment"
    if not (worker_image or "").strip():
        return "worker_image is empty"
    if not worker_image_repo():
        return "FUBALA_WORKER_IMAGE_REPO is not set"
    try:
        import kubernetes  # noqa: F401
    except Exception as e:  # noqa: BLE001 - any import failure means "no pods"
        return f"kubernetes client import failed: {e}"
    return None


class RealKubeApi:
    """The kubernetes-client backed KubeApi (lazy import, in-cluster config)."""

    def __init__(self) -> None:
        from kubernetes import client, config

        config.load_incluster_config()
        self._client = client
        self._core = client.CoreV1Api()
        self.namespace = (SA_DIR / "namespace").read_text().strip()

    def create_pod(self, body: dict[str, Any]) -> None:
        self._core.create_namespaced_pod(self.namespace, body)

    def read_pod(self, name: str) -> dict[str, Any] | None:
        from kubernetes.client.exceptions import ApiException

        try:
            pod = self._core.read_namespaced_pod(name, self.namespace)
        except ApiException as e:
            if e.status == 404:
                return None
            raise
        return {"phase": pod.status.phase, "node": pod.spec.node_name}

    def delete_pod(self, name: str) -> None:
        from kubernetes.client.exceptions import ApiException

        try:
            self._core.delete_namespaced_pod(name, self.namespace, grace_period_seconds=0)
        except ApiException as e:
            if e.status != 404:
                raise

    def list_pods(self, label_selector: str) -> list[dict[str, Any]]:
        res = self._core.list_namespaced_pod(self.namespace, label_selector=label_selector)
        return [
            {
                "name": p.metadata.name,
                "story": (p.metadata.labels or {}).get(story_label()),
                "node": p.spec.node_name,
                "phase": p.status.phase,
            }
            for p in res.items
        ]

    def exec(
        self,
        name: str,
        argv: list[str],
        stdin_bytes: bytes | None,
        on_output: Callable[[str], None] | None,
        timeout: float,
    ) -> tuple[int, str, str]:
        from kubernetes.stream import stream

        deadline = time.monotonic() + timeout
        ws = stream(
            self._core.connect_get_namespaced_pod_exec,
            name,
            self.namespace,
            command=argv,
            stderr=True,
            stdin=stdin_bytes is not None,
            stdout=True,
            tty=False,
            _preload_content=False,
        )
        out: list[str] = []
        err: list[str] = []
        try:
            if stdin_bytes is not None:
                # the exec stdin has no reliable EOF: the payload is
                # length-prefixed and the remote side reads exactly that much
                for i in range(0, len(stdin_bytes), 1 << 16):
                    ws.write_stdin(stdin_bytes[i : i + (1 << 16)])
            while ws.is_open():
                if time.monotonic() > deadline:
                    raise TimeoutError(f"exec in {name} exceeded {timeout:.0f}s")
                ws.update(timeout=1)
                while ws.peek_stdout():
                    chunk = ws.read_stdout()
                    out.append(chunk)
                    if on_output is not None:
                        on_output(chunk)
                while ws.peek_stderr():
                    err.append(ws.read_stderr())
            rc = ws.returncode
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                log.debug("closing exec stream failed", exc_info=True)
        return (rc if isinstance(rc, int) else 1), "".join(out), "".join(err)


_kube_factory: Callable[[], KubeApi] = RealKubeApi


def set_kube_factory(factory: Callable[[], KubeApi] | None) -> None:
    """Tests inject a fake client here (None restores the real one)."""
    global _kube_factory
    _kube_factory = factory or RealKubeApi


# ---- pod spec -------------------------------------------------------------------


def pod_deadline(settings: dict[str, Any], mode: str = "verify") -> int:
    """activeDeadlineSeconds: the coder headroom plus EVERY verify round. In
    pod mode every round's coder also runs in the pod, so the headless
    timeout counts once per round."""
    headless_s = float(settings.get("headless_timeout_seconds", 2400))
    verify = float(settings.get("verify_timeout", 600))
    rounds = 1 + int(settings.get("verify_fix_rounds", 2))
    coder_rounds = rounds if mode == "pod" else 1
    return int(math.ceil(headless_s * coder_rounds + verify * rounds))


_CPU_RE = re.compile(r"^(?:(\d+)m|(\d+(?:\.\d+)?))$")
_MEM_RE = re.compile(r"^(\d+)(Mi|Gi)$")


def parse_cpu_millicores(value: str) -> int:
    """A Kubernetes CPU quantity ("1500m", "4", "1.5") in millicores."""
    m = _CPU_RE.match(value or "")
    if not m:
        raise ValueError(f"bad CPU quantity: {value!r} (use e.g. 1500m or 4)")
    return int(m.group(1)) if m.group(1) is not None else round(float(m.group(2)) * 1000)


def parse_memory_bytes(value: str) -> int:
    """A Kubernetes memory quantity ("2Gi", "512Mi") in bytes."""
    m = _MEM_RE.match(value or "")
    if not m:
        raise ValueError(f"bad memory quantity: {value!r} (use e.g. 512Mi or 2Gi)")
    return int(m.group(1)) * (1 << 20 if m.group(2) == "Mi" else 1 << 30)


def build_pod_spec(
    *,
    name: str,
    story_id: str,
    image: str,
    owner_name: str,
    owner_uid: str,
    node_name: str,
    deadline_seconds: int,
    cpu_request: str = DEFAULTS["worker_cpu_request"],
    cpu_limit: str = DEFAULTS["worker_cpu_limit"],
    memory_request: str = DEFAULTS["worker_memory_request"],
    memory_limit: str = DEFAULTS["worker_memory_limit"],
    story_label: str = DEFAULT_STORY_LABEL,
    dev_node_label: str = "",
) -> dict[str, Any]:
    """The worker pod manifest. Pure: no client, no os.environ.

    With a dev_node_label the pod tolerates that node's taint and prefers it
    (the backend node stays allowed); without one there is no scheduling hint.
    """
    scheduling: dict[str, Any] = {}
    if dev_node_label:
        dev = {"key": dev_node_label, "operator": "In", "values": ["true"]}
        scheduling = {
            "tolerations": [
                {
                    "key": dev_node_label,
                    "operator": "Equal",
                    "value": "true",
                    "effect": "NoSchedule",
                }
            ],
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {"matchExpressions": [dev]},
                            {
                                "matchExpressions": [
                                    {
                                        "key": "kubernetes.io/hostname",
                                        "operator": "In",
                                        "values": [node_name],
                                    }
                                ]
                            },
                        ]
                    },
                    "preferredDuringSchedulingIgnoredDuringExecution": [
                        {"weight": 100, "preference": {"matchExpressions": [dev]}}
                    ],
                }
            },
        }
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "labels": {MANAGED_LABEL: MANAGED_VALUE, story_label: story_id},
            "ownerReferences": [
                {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "name": owner_name,
                    "uid": owner_uid,
                    "blockOwnerDeletion": False,
                    "controller": False,
                }
            ],
        },
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "activeDeadlineSeconds": deadline_seconds,
            **scheduling,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": WORKER_UID,
                "runAsGroup": WORKER_UID,
                "fsGroup": WORKER_UID,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "worker",
                    "image": image,
                    "command": ["sh", "-c", watchdog_script()],
                    "env": [{"name": k, "value": v} for k, v in POD_ENV.items()],
                    "resources": {
                        "requests": {"cpu": cpu_request, "memory": memory_request},
                        "limits": {"cpu": cpu_limit, "memory": memory_limit},
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "runAsNonRoot": True,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "volumeMounts": [
                        {"name": "work", "mountPath": "/work"},
                        {"name": "cache", "mountPath": "/cache"},
                    ],
                }
            ],
            "volumes": [
                {"name": "work", "emptyDir": {}},
                {"name": "cache", "emptyDir": {}},
            ],
        },
    }


# ---- exec wrappers --------------------------------------------------------------

_EXEC_OUTER = (
    'cd "$1" || exit 125; cmd=$2; '
    'mkdir -p "$HOME" /work/.exec; touch ' + HEARTBEAT + "; "
    "exec setsid --wait sh -c "
    '\'echo $$ > "$0"; exec timeout --kill-after=10 {t} sh -c "$1" </dev/null\' '
    '/work/.exec/{n}.pgid "$cmd"'
)

# stdin: "<N>\n" then N bytes of git bundle; $1 = snapshot ref, $2 = sha,
# $3 (optional, headless) = the snapshot's parent: `reset -q` to it so the
# pod's HEAD equals the local HEAD and uncommitted work stays uncommitted
SYNC_SCRIPT = (
    "set -e; mkdir -p /work/.exec; touch " + HEARTBEAT + "; "
    'read -r n; head -c "$n" > /work/.bundle; '
    "[ -d " + REPO_DIR + "/.git ] || git init -q " + REPO_DIR + "; "
    "cd " + REPO_DIR + "; "
    'if [ "$n" -gt 0 ]; then '
    'git fetch -q --no-tags /work/.bundle "+$1:refs/fubala/snap"; fi; '
    'git checkout -q --force --detach "$2"; git clean -fdq; rm -f /work/.bundle; '
    'if [ -n "${3:-}" ]; then git reset -q "$3"; fi'
)

# The headless CLI run. argv: $1 = cwd, $2 = exec number; NEITHER the token
# nor the prompt (exec argv ends up in API-server URLs and audit logs). stdin
# is a length-prefixed frame (`headless_frame`):
#   "<timeout> <n_env> <n_args>\n"
#   n_env x "<NAME> <len>\n" + <len> bytes
#   n_args x "<len>\n" + <len> bytes          (the CLI argv, prompt included)
# parsed with shell builtins and `head -c` (which never reads past <len>);
# the values are exported into the CLI's environment only: never written to
# a file, never echoed. The trailing `x` keeps trailing newlines intact.
HEADLESS_SCRIPT = (
    'cd "$1" || exit 125; n=$2; mkdir -p /work/.exec; touch ' + HEARTBEAT + "; "
    "read -r t ne na || exit 125; "
    'i=0; while [ "$i" -lt "$ne" ]; do '
    'read -r k len || exit 125; v=$(head -c "$len"; printf x); '
    'case "$k" in [A-Za-z_]*) export "$k=${v%x}" ;; *) exit 125 ;; esac; '
    "i=$((i + 1)); done; "
    'set --; i=0; while [ "$i" -lt "$na" ]; do '
    'read -r len || exit 125; v=$(head -c "$len"; printf x); '
    'set -- "$@" "${v%x}"; i=$((i + 1)); done; unset v; '
    'mkdir -p "$HOME" "$CLAUDE_CONFIG_DIR"; '
    "exec setsid --wait sh -c "
    '\'echo $$ > "$0"; t=$1; shift; exec timeout --kill-after=10 "$t" "$@" </dev/null\' '
    '"/work/.exec/$n.pgid" "$t" "$@"'
)

# $1 = the round's start sha. stdout: "head <sha>\n", and when HEAD moved
# "len <N>\n" + N chars of base64 bundle (start..HEAD) + BUNDLE_END line:
# exec stdout is text, so the binary bundle travels base64-encoded.
PULL_SCRIPT = (
    "cd " + REPO_DIR + " || exit 125; touch " + HEARTBEAT + "; "
    'h=$(git rev-parse HEAD) || exit 125; echo "head $h"; '
    '[ "$h" = "$1" ] && exit 0; '
    'git bundle create /work/.pull.bundle "$1..HEAD" >&2 || exit 125; '
    "base64 /work/.pull.bundle > /work/.pull.b64 && "
    'echo "len $(wc -c < /work/.pull.b64)" && cat /work/.pull.b64 && '
    "echo " + BUNDLE_END + "; rc=$?; rm -f /work/.pull.bundle /work/.pull.b64; exit $rc"
)

# $1 = a round's CLAUDE_CONFIG_DIR, $2 = byte cap: the newest session JSONL
# (exit 3 when the CLI wrote none)
TRANSCRIPT_SCRIPT = (
    'fs=$(find "$1/projects" -type f -name "*.jsonl" 2>/dev/null); '
    '[ -n "$fs" ] || exit 3; f=$(ls -t $fs | head -n 1); head -c "$2" "$f"'
)

KILL_SCRIPT = (
    'for f in /work/.exec/*.pgid; do [ -f "$f" ] && kill -TERM -- "-$(cat "$f")" '
    "2>/dev/null; done; sleep 10; "
    'for f in /work/.exec/*.pgid; do [ -f "$f" ] && kill -KILL -- "-$(cat "$f")" '
    "2>/dev/null; done; true"
)


REAP_SCRIPT = reap_script()
REAP_TIMEOUT = 20.0  # bound on the reap exec after a run
CLOSE_REAP_TIMEOUT = 15.0  # ... and on cancel/kill


def parse_reap_output(out: str, literals: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """REAP_SCRIPT stdout -> ``{command, pid, age_s}`` entries (command
    redacted of `literals`). Malformed lines are skipped."""
    entries: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        try:
            pid, age = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        comm, argv = parts[2], parts[3].strip()
        command = f"{comm} ({argv})" if argv else f"[{comm}]"
        entries.append({"command": headless._redact(command, literals), "pid": pid, "age_s": age})
    return entries


def exec_argv(command: str, cwd: str, timeout: float, n: int) -> list[str]:
    script = _EXEC_OUTER.format(t=max(1, int(math.ceil(timeout))), n=n)
    return ["sh", "-c", script, "_", cwd, command]


def headless_frame(timeout: float, env: dict[str, str], argv: list[str]) -> bytes:
    """The HEADLESS_SCRIPT stdin frame (see there); lengths are in bytes."""
    out = [f"{max(1, int(math.ceil(timeout)))} {len(env)} {len(argv)}\n".encode()]
    for key, value in env.items():
        raw = value.encode()
        out += [f"{key} {len(raw)}\n".encode(), raw]
    for arg in argv:
        raw = arg.encode()
        out += [f"{len(raw)}\n".encode(), raw]
    return b"".join(out)


def parse_pull_output(out: str) -> tuple[str, str | None]:
    """PULL_SCRIPT stdout -> (pod head, base64 bundle text or None when HEAD
    did not move). Raises ValueError on a malformed frame."""
    head_line, _, rest = out.partition("\n")
    if not head_line.startswith("head "):
        raise ValueError("no head line")
    pod_head = head_line[5:].strip()
    if not rest.strip():
        return pod_head, None
    len_line, _, rest = rest.partition("\n")
    if not len_line.startswith("len "):
        raise ValueError("no length line")
    n = int(len_line[4:].strip())
    data, tail = rest[:n], rest[n:]
    if len(data) != n or tail.strip() != BUNDLE_END:
        raise ValueError("truncated bundle frame")
    return pod_head, data


class _LineBuffer:
    """Re-split exec stdout chunks into whole lines for the stream reader."""

    def __init__(self, sink: Callable[[str], None]) -> None:
        self._sink = sink
        self._buf = ""

    def feed(self, chunk: str) -> None:
        self._buf += chunk
        *lines, self._buf = self._buf.split("\n")
        for line in lines:
            self._sink(line + "\n")

    def flush(self) -> None:
        if self._buf:
            rest, self._buf = self._buf, ""
            self._sink(rest)


def add_maxprocesses(command: str) -> str:
    """Cap xdist in pod commands: pytest segments with -n/--numprocesses get
    --maxprocesses=4 appended (so `-n auto` cannot read the node's core count)."""
    from .agent_tools import _SEGMENT_SPLIT_RE, _XDIST_FLAG_RE

    parts = _SEGMENT_SPLIT_RE.split(command)
    for i in range(0, len(parts), 2):
        seg = parts[i]
        if "--maxprocesses" in seg:
            continue
        if not any(t.rsplit("/", 1)[-1] == "pytest" for t in seg.split()):
            continue
        if not _XDIST_FLAG_RE.search(seg):
            continue
        stripped = seg.rstrip()
        parts[i] = f"{stripped} --maxprocesses={MAX_PROCESSES}{seg[len(stripped) :]}"
    return "".join(parts)


# ---- the executor ---------------------------------------------------------------

_WARNED: set[str] = set()
_WARN_LOCK = threading.Lock()


def warn_once(cause: str, message: str) -> None:
    """One log.warning per process per cause."""
    with _WARN_LOCK:
        if cause in _WARNED:
            return
        _WARNED.add(cause)
    log.warning("worker pod fallback (%s): %s", cause, message)


_FALLBACK_LOCK = threading.Lock()
_FALLBACK_HELD: set[str] = set()


def try_claim_fallback(story_id: str, cap: int, kube: KubeApi | None) -> bool:
    """Claim one of `cap` slots on the backend node for this story. The count
    is the union by story id of live managed pods placed on the backend node
    and the held claims, minus the caller. A failing pod listing counts the
    held claims only: a broken API must not stall the board."""
    with _FALLBACK_LOCK:
        counted = set(_FALLBACK_HELD)
        if kube is not None:
            try:
                for pod in kube.list_pods(MANAGED_SELECTOR):
                    if pod.get("node") == backend_node() and pod.get("phase") not in (
                        "Succeeded",
                        "Failed",
                    ):
                        counted.add(pod.get("story") or pod["name"])
            except Exception as e:  # noqa: BLE001
                warn_once("fallback-list", f"listing pods for the fallback cap failed: {e}")
        counted.discard(story_id)
        if len(counted) >= cap:
            return False
        _FALLBACK_HELD.add(story_id)
        return True


def release_fallback(story_id: str) -> None:
    with _FALLBACK_LOCK:
        _FALLBACK_HELD.discard(story_id)


_LIVE: dict[str, PodExecutor] = {}
_LIVE_LOCK = threading.Lock()


class PodExecutor:
    """One pod per story run, created lazily by `prepare()`, deleted by
    `close()` (and by kill_story / the sweeps)."""

    is_pod = True

    def __init__(
        self,
        story_id: str,
        settings: dict[str, Any],
        *,
        story_log: Callable[[str], None] | None = None,
        on_output: Callable[[str], None] | None = None,
        kube_factory: Callable[[], KubeApi] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        mode: str = "verify",
    ) -> None:
        self.story_id = story_id
        self.mode = mode
        self.settings = settings
        self.node_name: str | None = None
        self.pod_name: str | None = None
        self.disabled = False
        self._story_log = story_log
        self._on_output = on_output
        self._kube_factory = kube_factory
        self._kube: KubeApi | None = None
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()  # serializes prepare/sync/run
        self._state = threading.Lock()  # guards the pod identity; never held across IO
        self._synced_sha: str | None = None
        self._exec_n = 0
        self._logged = False
        self._claimed = False
        self._closed = False
        self._in_exec = False  # a run() exec is in flight (close() then reaps)
        # the in-flight headless run (config dir, literals, config), for the
        # transcript copy on cancel/kill
        self._headless: tuple[str, tuple[str, ...], headless.HeadlessConfig] | None = None

    # -- fallback plumbing

    def _log_line(self, text: str) -> None:
        if self._story_log is not None:
            try:
                self._story_log(text)
            except Exception:  # noqa: BLE001 - a log line never fails a run
                log.debug("story log line failed", exc_info=True)

    def fallback(self, cause: str, message: str) -> PodUnavailable:
        """Disable the pod path for the rest of the run; return the exception
        to raise (the pod is deleted first)."""
        warn_once(cause, message)
        if not self.disabled:
            self.disabled = True
            self._log_line(f"[verify] worker pod unavailable ({message}); running in-process\n")
        self._delete_pod()
        return PodUnavailable(message)

    def _api(self) -> KubeApi:
        if self._kube is None:
            factory = self._kube_factory or _kube_factory
            try:
                self._kube = factory()
            except Exception as e:  # noqa: BLE001
                raise self.fallback("client", f"kubernetes client unavailable: {e}") from e
        return self._kube

    # -- lifecycle

    def prepare(self) -> None:
        with self._lock:
            if self.disabled:
                raise PodUnavailable("pod path disabled for this run")
            if self.pod_name is not None and self.node_name is not None:
                return
            kube = self._api()
            if self.pod_name is None:
                self._create(kube)
            self._wait_running(kube)

    def _setting(self, key: str) -> str:
        return self.settings.get(key) or DEFAULTS[key]

    def _create(self, kube: KubeApi) -> None:
        name = f"fubala-verify-{re.sub('[^a-z0-9]', '', self.story_id.lower())[:12]}-" + (
            secrets.token_hex(3)
        )
        image_tag = (self.settings.get("worker_image") or "").strip()
        body = build_pod_spec(
            name=name,
            story_id=self.story_id,
            image=f"{worker_image_repo()}:{image_tag}",
            story_label=story_label(),
            dev_node_label=dev_node_label(),
            owner_name=os.environ.get("POD_NAME", ""),
            owner_uid=os.environ.get("POD_UID", ""),
            node_name=backend_node(),
            deadline_seconds=pod_deadline(self.settings, self.mode),
            cpu_request=self._setting("worker_cpu_request"),
            cpu_limit=self._setting("worker_cpu_limit"),
            memory_request=self._setting("worker_memory_request"),
            memory_limit=self._setting("worker_memory_limit"),
        )
        try:
            kube.create_pod(body)
        except Exception as e:  # noqa: BLE001 - 403 or any API error
            raise self.fallback("create", f"creating the worker pod failed: {e}") from e
        self.pod_name = name
        with _LIVE_LOCK:
            _LIVE[self.story_id] = self

    def _wait_running(self, kube: KubeApi) -> None:
        assert self.pod_name is not None
        started = self._clock()
        while True:
            try:
                state = kube.read_pod(self.pod_name)
            except Exception as e:  # noqa: BLE001
                raise self.fallback("api", f"reading the worker pod failed: {e}") from e
            if state is None:
                raise self.fallback("gone", "the worker pod disappeared")
            phase = state.get("phase")
            node = state.get("node") or ""
            if node and node == backend_node() and not self._claimed:
                cap = int(self.settings.get("worker_fallback_parallel") or 2)
                if not try_claim_fallback(self.story_id, cap, kube):
                    self._delete_pod()
                    raise FallbackCapFull(f"fallback cap {cap} reached on {node}")
                self._claimed = True
            if phase == "Running":
                self.node_name = state.get("node") or ""
                if not self._logged:
                    self._logged = True
                    kind = "backend node" if self.node_name == backend_node() else "dev node"
                    self._log_line(
                        f"[verify] verify pod {self.pod_name} on node {self.node_name} ({kind})\n"
                    )
                return
            if phase in ("Failed", "Succeeded"):
                raise self.fallback("ended", f"the worker pod ended early ({phase})")
            if self._clock() - started >= PENDING_LIMIT:
                raise self.fallback(
                    "pending", f"the worker pod was still Pending after {PENDING_LIMIT:.0f}s"
                )
            self._sleep(2.0)

    def _delete_pod(self) -> None:
        with self._state:
            name, self.pod_name, self.node_name = self.pod_name, None, None
            self._synced_sha = None
        self._claimed = False
        release_fallback(self.story_id)
        with _LIVE_LOCK:
            if _LIVE.get(self.story_id) is self:
                _LIVE.pop(self.story_id, None)
        if name is None or self._kube is None:
            return
        try:
            self._kube.delete_pod(name)
        except Exception:  # noqa: BLE001 - the ownerReference and the sweep catch leftovers
            log.warning("deleting worker pod %s failed", name, exc_info=True)

    def close(self) -> None:
        # deliberately not under self._lock: an in-flight run()/sync holds it
        # for the whole exec, and cancel must delete the pod right away (the
        # blocked exec then fails and falls back). An in-flight headless run
        # is killed in-pod first and its transcript copied (bounded), so the
        # session stays inspectable after a cancel.
        self._closed = True
        active = self._headless
        if active is not None:
            self.kill()
            config_dir, literals, config = active
            self._keep_transcript(config_dir, config, literals, CLOSE_TRANSCRIPT_TIMEOUT)
        if active is not None or self._in_exec:
            if active is None:
                self.kill()
            # what the killed exec left behind (pod deletion stays the backstop)
            self._reap("cancel", active[1] if active else (), CLOSE_REAP_TIMEOUT)
        self._delete_pod()

    def _reap(
        self, reason: str, literals: tuple[str, ...] = (), timeout: float = REAP_TIMEOUT
    ) -> None:
        """Kill whatever the last exec left in the pod and report it as story
        events. Bounded; every failure is logged and swallowed."""
        name, kube = self.pod_name, self._kube
        if name is None or kube is None:
            return
        try:
            rc, out, _err = kube.exec(name, ["sh", "-c", REAP_SCRIPT], None, None, timeout)
            if rc != 0:
                log.warning("reaping worker pod %s exited %s", name, rc)
                return
            procs.report(self.story_id, reason, parse_reap_output(out, literals))
        except Exception:  # noqa: BLE001 - reaping never changes a run's result
            log.warning("reaping worker pod %s failed", name, exc_info=True)

    # -- exec

    def _exec(
        self,
        argv: list[str],
        stdin: bytes | None,
        timeout: float,
        on_output: Callable[[str], None] | None = None,
    ) -> tuple[int, str, str]:
        name, kube = self.pod_name, self._kube
        if name is None or kube is None:  # closed (cancel/kill) under our feet
            raise self.fallback("closed", "the worker pod was closed")
        try:
            return kube.exec(name, argv, stdin, on_output, timeout)
        except TimeoutError as e:
            raise ExecTimeout(str(e)) from e
        except Exception as e:  # noqa: BLE001 - transport failure
            raise self.fallback("exec", f"exec in the worker pod failed: {e}") from e

    def sync_workspace(self, workspace: Path, reset_to: str | None = None) -> None:
        """Ship the worktree state (uncommitted and untracked included).
        `reset_to` (headless): `git reset -q` the pod to it afterwards."""
        with self._lock:
            self.prepare()
            try:
                bundle, sha = workspaces.snapshot_bundle(
                    workspace, self.story_id, since=self._synced_sha
                )
            except workspaces.WorkspaceError as e:
                raise self.fallback("bundle", f"snapshot bundle failed: {e}") from e
            payload = f"{len(bundle)}\n".encode() + bundle
            argv = [
                "sh", "-c", SYNC_SCRIPT, "_",
                f"{workspaces.SNAPSHOT_REF_PREFIX}{self.story_id}", sha,
            ]  # fmt: skip
            if reset_to:
                argv.append(reset_to)
            try:
                rc, _out, err = self._exec(argv, payload, 120.0 + len(bundle) / 1e6)
            except ExecTimeout as e:
                raise self.fallback("sync", f"workspace sync timed out: {e}") from e
            if rc != 0:
                raise self.fallback("sync", f"workspace sync failed (rc {rc}): {err[-300:]}")
            self._synced_sha = sha

    def run(
        self,
        command: str,
        cwd_rel: str,
        env: dict[str, str] | None,
        timeout: float,
        on_output: Callable[[str], None] | None = None,
    ) -> tuple[int, str]:
        # `env` is ignored on purpose: the pod env is the explicit allowlist
        # of the spec, nothing from the backend travels in
        with self._lock:
            self.prepare()
            self._exec_n += 1
            cwd = f"{REPO_DIR}/{cwd_rel}".rstrip("/") if cwd_rel else REPO_DIR
            argv = exec_argv(command, cwd, timeout, self._exec_n)
            emit = on_output or self._on_output
            self._in_exec = True
            try:
                rc, out, err = self._exec(argv, None, timeout + EXEC_MARGIN, emit)
            except ExecTimeout:
                # the outer bound expired: delete the pod (a later call gets a
                # fresh one) and report today's timeout result
                warn_once("exec-timeout", "an exec outlived its bound; pod deleted")
                self._reap("timeout")
                self._delete_pod()
                return 124, f"verify timed out after {timeout}s"
            finally:
                self._in_exec = False
            self._reap("timeout" if rc == 124 else "round_end")
        output = out[-MAX_OUTPUT:]
        if err.strip():
            output += f"\nstderr:\n{err[-MAX_OUTPUT:]}"
        if rc == 124:
            output += f"\nverify timed out after {timeout}s"
        return rc, output

    # -- headless (run_executor=pod)

    def _exec_started(
        self,
        argv: list[str],
        stdin: bytes | None,
        timeout: float,
        literals: tuple[str, ...],
        on_output: Callable[[str], None] | None = None,
    ) -> tuple[int, str, str]:
        """An exec after the headless run started: failures are PodRunLost
        (never a fallback), every text redacted of the secret literal."""
        name, kube = self.pod_name, self._kube
        if name is None or kube is None or self._closed:
            raise PodRunLost("the worker pod was closed")
        try:
            return kube.exec(name, argv, stdin, on_output, timeout)
        except TimeoutError as e:
            raise ExecTimeout(headless._redact(str(e), literals)) from None
        except Exception as e:  # noqa: BLE001 - transport failure
            raise PodRunLost(headless._redact(f"exec failed: {e}", literals)) from None

    def git(self, *args: str, ok_codes: tuple[int, ...] = ()) -> str:
        """`git -C /work/repo ...` in the pod (workspaces.GitFn)."""
        literals = self._headless[1] if self._headless else ()
        try:
            rc, out, err = self._exec_started(["git", "-C", REPO_DIR, *args], None, 120.0, literals)
        except ExecTimeout as e:
            raise PodRunLost(f"git {' '.join(args[:2])} timed out: {e}") from None
        if rc != 0 and rc not in ok_codes:
            raise workspaces.WorkspaceError(
                f"git {' '.join(args[:2])}: {headless._redact(err, literals).strip()[:300]}"
            )
        return out

    def _keep_transcript(
        self,
        config_dir: str,
        config: headless.HeadlessConfig,
        literals: tuple[str, ...],
        timeout: float = 60.0,
    ) -> Path | None:
        """`cat` the round's newest session JSONL (size-capped) out of the
        pod and keep it like the local path does. Best effort."""
        if not config.artifacts_dir:
            return None
        name, kube = self.pod_name, self._kube
        if name is None or kube is None:
            return None
        argv = ["sh", "-c", TRANSCRIPT_SCRIPT, "_", config_dir, str(TRANSCRIPT_CAP)]
        try:
            rc, out, _err = kube.exec(name, argv, None, None, timeout)
        except Exception:  # noqa: BLE001 - the transcript never fails a run
            log.warning("copying the headless transcript out of %s failed", name)
            return None
        if rc != 0:
            return None
        fd, tmp = tempfile.mkstemp(prefix="fubala-pod-transcript-", suffix=".jsonl")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(out)
            return headless.keep_transcript(
                Path(tmp), self.story_id, Path(config.artifacts_dir), literals
            )
        except OSError:
            return None
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _commit_and_pull(
        self,
        project_root: str,
        workspace: Path,
        start: str,
        commit_message: str,
        on_artefacts: Callable[[list[str]], None] | None,
        literals: tuple[str, ...],
    ) -> None:
        """Commit the round in the pod (shared artefact guard), then
        fast-forward the local worktree from a bundle of start..HEAD."""
        try:
            workspaces.commit(
                project_root, self.story_id, commit_message, on_artefacts, git=self.git
            )
            try:
                rc, out, err = self._exec_started(
                    ["sh", "-c", PULL_SCRIPT, "_", start], None, 300.0, literals
                )
            except ExecTimeout as e:
                raise PodRunLost(f"pull-back timed out: {e}") from None
            if rc != 0:
                raise PodRunLost(
                    f"pull-back failed (rc {rc}): {headless._redact(err, literals)[-300:]}"
                )
            try:
                new, data = parse_pull_output(out)
            except ValueError as e:
                raise PodRunLost(f"pull-back output malformed: {e}") from None
            if data is None:
                return  # HEAD did not move: nothing to pull
            bundle = base64.b64decode(data)
            workspaces.pull_bundle(workspace, self.story_id, bundle, start, new)
        except workspaces.WorkspaceError as e:
            raise PodRunLost(headless._redact(f"commit/pull-back: {e}", literals)) from None

    def run_headless(
        self,
        prompt: str,
        config: headless.HeadlessConfig,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        *,
        workspace: Path,
        project_root: str,
        commit_message: str,
        on_artefacts: Callable[[list[str]], None] | None = None,
    ) -> headless.HeadlessOutcome:
        """One headless CODE round in the pod.

        PodUnavailable before the exec starts (the caller then runs the round
        locally); PodRunLost after it started. On success the round is
        committed in the pod with `commit_message` and the local worktree is
        fast-forwarded to it, so the runner's own commit is a no-op."""
        literals = headless.run_secrets(config)
        with self._lock:
            self.prepare()
            try:
                start = workspaces.worktree_head(workspace)
            except workspaces.WorkspaceError as e:
                raise self.fallback("bundle", f"reading the local HEAD failed: {e}") from e
            self.sync_workspace(workspace, reset_to=start)
            # ---- started: from here on no fallback, only PodRunLost
            self._exec_n += 1
            n = self._exec_n
            config_dir = f"{CLAUDE_DIR}/{n}"
            env = headless.pod_child_env(config, config_dir)
            child_argv = headless.child_argv(prompt, config)
            if config.mcp_config:
                # the graph is built inside the pod (data under /tmp, never in
                # the repo) right before the CLI starts; fail open
                child_argv = code_graph.pod_argv(child_argv, REPO_DIR)
            frame = headless_frame(config.timeout_seconds, env, child_argv)
            argv = ["sh", "-c", HEADLESS_SCRIPT, "_", REPO_DIR, str(n)]
            stream = headless.StreamReader(literals, headless.safe_relay(on_event))
            lines = _LineBuffer(stream.feed)
            self._headless = (config_dir, literals, config)
            started = self._clock()
            timed_out = False
            try:
                try:
                    rc, _out, err = self._exec_started(
                        argv,
                        frame,
                        float(config.timeout_seconds) + EXEC_MARGIN,
                        literals,
                        lines.feed,
                    )
                except ExecTimeout:
                    warn_once("exec-timeout", "a headless exec outlived its bound")
                    timed_out = True
                    rc, err = 124, ""
                    self.kill()
                lines.flush()
                duration_ms = int((self._clock() - started) * 1000)
                if self._closed:
                    raise PodRunLost("the worker pod was closed mid-run")
                transcript = self._keep_transcript(config_dir, config, literals)
                # before the commit: a leftover must not touch the tree mid-commit
                self._reap("timeout" if rc == 124 else "round_end", literals)
            finally:
                self._headless = None
            if rc == 124:
                outcome = headless.timeout_outcome(duration_ms)
            else:
                outcome = headless.map_outcome(
                    exit_code=rc,
                    stderr=err,
                    stream=stream,
                    config=config,
                    duration_ms=duration_ms,
                    transcript_path=transcript,
                )
            self._commit_and_pull(
                project_root, workspace, start, commit_message, on_artefacts, literals
            )
            if timed_out:
                self._delete_pod()  # a later round gets a fresh pod
            return outcome

    def _spill(self, path: str, data: bytes) -> None:
        """Write `data` to `path` in the pod (an argv value past SPILL_BYTES
        would hit the kernel's per-argument limit; stdin has none)."""
        try:
            rc, _out, err = self._exec(
                ["sh", "-c", 'mkdir -p /work/.exec && cat > "$1"', "_", path],
                data,
                120.0,
            )
        except ExecTimeout as e:
            raise self.fallback("spill", f"writing a prompt file timed out: {e}") from e
        if rc != 0:
            raise self.fallback("spill", f"writing a prompt file failed (rc {rc}): {err[-300:]}")

    def _cli_argv(self, argv: list[str], prompt: str, n: int) -> list[str]:
        """The CLI argv for a pod session: the prompt goes after -p (the
        HEADLESS_SCRIPT child has stdin </dev/null). A prompt or system prompt
        over SPILL_BYTES goes through a file instead (stdin redirect /
        --system-prompt-file) because one argv entry is capped at 128 KiB."""
        argv = list(argv)
        if "--system-prompt" in argv:
            i = argv.index("--system-prompt") + 1
            system = argv[i].encode()
            if len(system) > SPILL_BYTES:
                path = f"/work/.exec/{n}.system"
                self._spill(path, system)
                argv[i - 1 : i + 1] = ["--system-prompt-file", path]
        raw = prompt.encode()
        if len(raw) <= SPILL_BYTES:
            argv.insert(argv.index("-p") + 1, prompt)
            return argv
        path = f"/work/.exec/{n}.prompt"
        self._spill(path, raw)
        return ["sh", "-c", 'f=$1; shift; exec "$@" <"$f"', "_", path, *argv]

    def run_cli_session(
        self,
        argv: list[str],
        env: dict[str, str],
        prompt: str,
        timeout: float,
        workspace: Path,
    ) -> tuple[int, str, str]:
        """One non-interactive `claude -p` session (spec phase, built by
        claude_code.build_cli_session) in the pod, against a snapshot of
        `workspace`. Read-only: nothing is committed or pulled back.

        Returns (rc, stdout, stderr) redacted of the token; rc 124 when the
        outer bound expired. PodUnavailable before the exec starts (the caller
        runs the session locally); PodRunLost after it started."""
        secret_keys = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")
        literals = tuple(v for k, v in env.items() if v and k in secret_keys)
        with self._lock:
            self.prepare()
            if timeout + EXEC_MARGIN > pod_deadline(self.settings, self.mode):
                raise self.fallback("deadline", "the session outlives the pod deadline")
            self.sync_workspace(workspace)
            self._exec_n += 1
            n = self._exec_n
            child = self._cli_argv(argv, prompt, n)
            # ---- started: from here on no fallback, only PodRunLost
            if "--mcp-config" in argv:
                child = code_graph.pod_argv(child, REPO_DIR)
            frame = headless_frame(
                timeout, {**env, "CLAUDE_CONFIG_DIR": f"{CLAUDE_DIR}/{n}"}, child
            )
            exec_argv_ = ["sh", "-c", HEADLESS_SCRIPT, "_", REPO_DIR, str(n)]
            self._in_exec = True
            try:
                try:
                    rc, out, err = self._exec_started(
                        exec_argv_, frame, timeout + EXEC_MARGIN, literals
                    )
                except ExecTimeout:
                    warn_once("exec-timeout", "a CLI session outlived its bound")
                    rc, out, err = 124, "", ""
                    self.kill()
                if self._closed:
                    raise PodRunLost("the worker pod was closed mid-run")
                self._reap("timeout" if rc == 124 else "round_end", literals)
            finally:
                self._in_exec = False
            if rc == 124:
                self._delete_pod()  # a later session gets a fresh pod
        return rc, headless._redact(out, literals), headless._redact(err, literals)

    def kill(self) -> None:
        """Graceful in-pod kill of running execs (TERM, KILL after 10s)."""
        name, kube = self.pod_name, self._kube
        if name is None or kube is None:
            return
        try:
            kube.exec(name, ["sh", "-c", KILL_SCRIPT], None, None, 40.0)
        except Exception:  # noqa: BLE001
            log.debug("in-pod kill failed", exc_info=True)

    def heartbeat(self) -> None:
        name, kube = self.pod_name, self._kube
        if name is None or kube is None:
            return
        try:
            kube.exec(name, ["touch", HEARTBEAT], None, None, 20.0)
        except Exception:  # noqa: BLE001 - the next beat (or the fallback) handles it
            log.debug("worker pod heartbeat failed", exc_info=True)


# ---- construction / hooks / sweeps ------------------------------------------------


def build_executor(
    mode: str,
    story_id: str,
    settings: dict[str, Any],
    *,
    story_log: Callable[[str], None] | None = None,
    on_output: Callable[[str], None] | None = None,
    kube_factory: Callable[[], KubeApi] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> PodExecutor | None:
    """A PodExecutor for run_executor verify|pod, or None when the run stays
    in-process (mode in_process, or a preflight failure: one warning per
    process, one story-log line per run)."""
    if mode not in ("verify", "pod"):
        return None
    if kube_factory is None and _kube_factory is RealKubeApi:
        reason = preflight(str(settings.get("worker_image") or ""))
    else:
        reason = _injected_preflight(settings)
    if reason is not None:
        warn_once(f"preflight:{reason}", reason)
        if story_log is not None:
            story_log(f"[verify] run_executor={mode} but {reason}; running in-process\n")
        return None
    if mode == "pod" and story_log is not None:
        story_log(
            "run_executor=pod: headless runs in the worker pod; "
            "pump tools stay in-process until step 2\n"
        )
    return PodExecutor(
        story_id,
        settings,
        story_log=story_log,
        on_output=on_output,
        kube_factory=kube_factory,
        clock=clock,
        sleep=sleep,
        mode=mode,
    )


def _injected_preflight(settings: dict[str, Any]) -> str | None:
    """With an injected client only the settings/env prerequisites apply."""
    missing = [v for v in ("POD_NAME", "POD_UID", "NODE_NAME") if not os.environ.get(v)]
    if missing:
        return f"{', '.join(missing)} not set in the backend environment"
    if not str(settings.get("worker_image") or "").strip():
        return "worker_image is empty"
    if not worker_image_repo():
        return "FUBALA_WORKER_IMAGE_REPO is not set"
    return None


def _kill_story_pod(story_id: str) -> None:
    with _LIVE_LOCK:
        ex = _LIVE.get(story_id)
    if ex is not None:
        # off-thread: cancel/stop are async paths and the API call may block
        threading.Thread(target=ex.close, daemon=True).start()


def _kill_all_pods() -> None:
    with _LIVE_LOCK:
        live = list(_LIVE.values())
    for ex in live:
        ex.close()


procs.set_pod_hooks(_kill_story_pod, _kill_all_pods)


def sweep_pods(running: set[str]) -> list[str]:
    """Delete managed pods whose story is not running here; returns the names.

    Pods of live executors of this process are spared. A no-op outside a
    cluster (no token), so dev boxes and tests stay quiet."""
    if _kube_factory is RealKubeApi and not in_cluster():
        return []
    try:
        kube = _kube_factory()
        pods = kube.list_pods(MANAGED_SELECTOR)
    except Exception:  # noqa: BLE001 - a failed sweep never blocks anything
        log.warning("worker pod sweep could not list pods", exc_info=True)
        return []
    with _LIVE_LOCK:
        spared = {ex.pod_name for ex in _LIVE.values() if ex.pod_name}
    deleted: list[str] = []
    for pod in pods:
        if pod["name"] in spared or pod.get("story") in running:
            continue
        try:
            kube.delete_pod(pod["name"])
            deleted.append(pod["name"])
            log.warning("pod sweep: deleted worker pod %s", pod["name"])
        except Exception:  # noqa: BLE001
            log.warning("pod sweep: deleting %s failed", pod["name"], exc_info=True)
    return deleted


def heartbeat_all() -> None:
    with _LIVE_LOCK:
        live = list(_LIVE.values())
    for ex in live:
        ex.heartbeat()


__all__ = [
    "FallbackCapFull",
    "PodExecutor",
    "PodRunLost",
    "PodUnavailable",
    "add_maxprocesses",
    "build_executor",
    "build_pod_spec",
    "release_fallback",
    "sweep_pods",
    "try_claim_fallback",
]
