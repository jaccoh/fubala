"""Guard for the CI timeout dump in .gitea/workflows/ci.yml.

Incident run 3098: the test pod hung mid-suite, the job log showed no
failures, and the pod kept burning CPU after the job gave up. Both wait
loops (test job and land job) must, on timeout, print each container's log
tail plus its FAILED/ERROR/RERUN lines before deleting the pod and secret.

These tests extract `dump_pod_logs` and the two timeout branches from the
workflow text and run them under a stub `kubectl` on PATH, so the shell is
exercised the way CI runs it (POSIX sh, and `set -e` for the land job).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CI = REPO / ".gitea" / "workflows" / "ci.yml"

# Pod names are `${...}-<run id>`; a fixed suffix is fine for the stub.
POD_TEST = "ci-fubala-12345"
POD_LAND = "land-fubala-12345"
SECRET = "git-netrc-12345"


def _block(text: str, start: str, fence: str) -> str:
    """Lines from `start` through the first later line starting with `fence`
    (inclusive: the closing `}` of the function / closing `fi` of the if)."""
    lines = text.splitlines()
    i = next(n for n, ln in enumerate(lines) if ln.lstrip().startswith(start))
    j = next(n for n in range(i, len(lines)) if lines[n].startswith(fence))
    return "\n".join(lines[i : j + 1])


def extract_dump_fn(text: str) -> str:
    """The shared dump_pod_logs() body (defined twice; identical)."""
    return _block(text, "dump_pod_logs() {", "          }")


def extract_test_timeout(text: str) -> str:
    """The test job's post-loop timeout branch."""
    return _block(text, "dump_pod_logs \"${POD}\"", "          exit 1")


def extract_land_timeout(text: str) -> str:
    """The land job's post-loop timeout branch."""
    return _block(text, 'if [ "${PHASE}" != "Succeeded" ]; then', "          fi")


def write_stub_kubectl(
    bindir: Path,
    log_by_container: dict[str, str],
    containers: tuple[str, ...] = ("git-clone", "backend", "frontend"),
) -> Path:
    """A stub kubectl that serves canned pod data and records every call."""
    calls = bindir / "kubectl-calls.txt"
    bodies = []
    for container, log in log_by_container.items():
        escaped = log.replace("\\", "\\\\").replace("'", "'\\''")
        bodies.append(
            f"  '{container}') printf '%s\\n' '{escaped}' ;;\n"
        )
    script = f"""#!/bin/sh
echo "$*" >> "{calls}"
case "$1" in
  get)
    case "$*" in
      *jsonpath=*.spec.initContainers*)
        echo "{' '.join(containers)}" ;;
      *jsonpath=*.status.phase*)
        echo "Running" ;;
      *)
        echo "stub: unexpected get $*" >&2
        exit 1 ;;
    esac ;;
  logs)
    C=""
    prev=""
    for a in "$@"; do
      [ "$prev" = "-c" ] && C="$a"
      prev="$a"
    done
    case "$C" in
{"".join(bodies)}      *) echo "stub: no container $C" >&2; exit 1 ;;
    esac ;;
  delete)
    exit 0 ;;
  *)
    exit 0 ;;
esac
"""
    bindir.mkdir(parents=True, exist_ok=True)
    kubectl = bindir / "kubectl"
    kubectl.write_text(script)
    kubectl.chmod(0o755)
    return calls


def run_sh(script: str, bindir: Path, set_e: bool) -> subprocess.CompletedProcess:
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
    body = ("set -e\n" if set_e else "") + script + "\necho EXITED_CLEANLY\n"
    return subprocess.run(
        ["/bin/sh", "-c", body],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        cwd=str(REPO),
    )


def make_script(dump_fn: str, branch: str, pod: str) -> str:
    return (
        f'POD="{pod}"\nSECRET="{SECRET}"\n\n'
        + dump_fn
        + "\n\n"
        + branch
    )


def long_log(name: str) -> str:
    """300 info lines, then the interesting ones past line 200."""
    lines = [f"INFO {name} line {n:04d}" for n in range(1, 301)]
    lines += [
        f"RERUN test_execution.py::test_{name}",
        f"FAILED tests/test_{name}.py::test_slow - AssertionError",
        f"ERROR tests/test_{name}.py::test_slow - timeout after 600s",
    ]
    return "\n".join(lines) + "\n"


def assert_dump_output(out: str, containers: tuple[str, ...]) -> None:
    # Every container is dumped, tail and grep.
    for container in containers:
        assert f"/ {container}: last 200 lines ===" in out, container
        assert f"/ {container}: FAILED/ERROR/RERUN ===" in out, container
        # Progress from deep inside each log reaches the job log, and the
        # FAILED/ERROR/RERUN lines sit past line 200 and are still grepped.
        assert f"INFO {container} line 0150" in out
        assert f"RERUN test_execution.py::test_{container}" in out
        assert f"FAILED tests/test_{container}.py::test_slow" in out
        assert f"ERROR tests/test_{container}.py::test_slow" in out


def test_test_job_timeout_dumps_then_cleans_up(tmp_path: Path) -> None:
    text = CI.read_text()
    calls = write_stub_kubectl(
        tmp_path / "bin",
        {
            "backend": long_log("backend"),
            "frontend": long_log("frontend"),
            "git-clone": long_log("git-clone"),
        },
    )
    script = make_script(
        extract_dump_fn(text), extract_test_timeout(text), POD_TEST
    )
    result = run_sh(script, tmp_path / "bin", set_e=False)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "EXITED_CLEANLY" not in result.stdout
    assert_dump_output(result.stdout, ("git-clone", "backend", "frontend"))
    assert "Timeout waiting for test pod (pod and secret cleaned)" in result.stdout
    call_log = calls.read_text()
    assert f"logs {POD_TEST}" in call_log
    assert f"delete pod {POD_TEST}" in call_log
    assert f"delete secret {SECRET}" in call_log
    # Dump happens BEFORE the deletes: once the pod is gone, so is the log.
    assert call_log.index("logs ") < call_log.index("delete pod")


def test_land_job_timeout_dumps_then_cleans_up_under_set_e(tmp_path: Path) -> None:
    text = CI.read_text()
    calls = write_stub_kubectl(
        tmp_path / "bin",
        {
            "land": long_log("land"),
            "frontend": long_log("frontend"),
        },
        containers=("land", "frontend"),
    )
    script = make_script(
        extract_dump_fn(text), extract_land_timeout(text), POD_LAND
    )
    # The land job run block starts with `set -e`: a dump that trips on a
    # failing kubectl/grep would abort mid-way and never reach the deletes.
    result = run_sh(script, tmp_path / "bin", set_e=True)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "EXITED_CLEANLY" not in result.stdout
    assert_dump_output(result.stdout, ("land", "frontend"))
    assert "Timeout waiting for land pod (pod and secret cleaned)" in result.stdout
    call_log = calls.read_text()
    assert f"delete pod {POD_LAND}" in call_log
    assert f"delete secret {SECRET}" in call_log
    assert call_log.index("logs ") < call_log.index("delete pod")


def test_dump_survives_a_container_whose_log_is_empty(tmp_path: Path) -> None:
    """A Pending/never-started container logs nothing; only headers print.

    git-clone is deliberately ABSENT from the stub's canned logs, so its
    `kubectl logs` call fails outright (exit 1): the || true guards must
    absorb that too, and the dump must still reach the deletes under
    `set -e` (the land job runs this exact function under set -e).
    """
    text = CI.read_text()
    calls = write_stub_kubectl(
        tmp_path / "bin",
        {"backend": "", "frontend": long_log("frontend")},
    )
    script = make_script(
        extract_dump_fn(text), extract_test_timeout(text), POD_TEST
    )
    result = run_sh(script, tmp_path / "bin", set_e=True)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "EXITED_CLEANLY" not in result.stdout
    # The empty container degrades to the no-match fallback...
    assert "no FAILED/ERROR/RERUN lines" in result.stdout
    # ...and the healthy container still gets its full dump.
    assert_dump_output(result.stdout, ("frontend",))
    assert f"delete pod {POD_TEST}" in calls.read_text()


def test_dump_fn_copies_do_not_drift() -> None:
    """The workflow has no shared scope, so the function is pasted into both
    job blocks. Guard the paste: a byte-identical function keeps the guards
    above honest -- exercising copy 1 would mean nothing about copy 2."""
    text = CI.read_text()
    blocks = text.split("dump_pod_logs() {")
    assert len(blocks) == 3, "expected exactly two dump_pod_logs definitions"
    first = blocks[1].split("          }")[0]
    second = blocks[2].split("          }")[0]
    assert first == second, "the two dump_pod_logs copies have drifted"
    for copy in (first, second):
        # Every kubectl/grep step is || true-guarded: the land job runs this
        # under set -e, and a no-match grep must not abort the dump midway.
        assert copy.count("|| true") == 4, copy
        assert copy.count("|| echo") == 1, copy


def test_workflow_defines_dump_and_uses_it_in_both_timeout_branches() -> None:
    text = CI.read_text()
    # One function per job block, each referenced exactly once after its loop.
    assert text.count("dump_pod_logs() {") == 2
    assert text.count('dump_pod_logs "${POD}"') == 2
    for marker in (
        "Timeout waiting for test pod (pod and secret cleaned)",
        "Timeout waiting for land pod (pod and secret cleaned)",
    ):
        assert marker in text
    # The Failed-phase branches keep the pod on purpose.
    assert "KEEPING the failed pod" in text
    assert "branch update or retest is RED; keeping" in text
