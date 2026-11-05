"""The pod exec scripts must return their command's real exit status.

The exec'd `sh` leads its process group (as it does in the pod), so a bare
util-linux `setsid` forks and its parent exits 0 at once. These tests
reproduce that mechanism: a group-leader shell (`start_new_session=True`)
running the script text with `/work/` pointed at a temp dir.
"""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import pods

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="needs util-linux setsid")


def _run(argv: list[str], tmp: Path, stdin: bytes = b"") -> subprocess.CompletedProcess:
    argv = list(argv)
    argv[2] = argv[2].replace("/work/", f"{tmp}/")
    (tmp / "home").mkdir(exist_ok=True)
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        timeout=30,
        start_new_session=True,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp / "home")},
    )


def test_exec_argv_returns_exit_status(tmp_path):
    res = _run(pods.exec_argv("exit 3", str(tmp_path), 30, 1), tmp_path)
    assert res.returncode == 3
    assert int((tmp_path / ".exec" / "1.pgid").read_text().strip()) > 0


def test_exec_argv_timeout_returns_124(tmp_path):
    start = time.monotonic()
    res = _run(pods.exec_argv("sleep 5", str(tmp_path), 1, 2), tmp_path)
    assert res.returncode == 124
    assert time.monotonic() - start < 4


def _headless(tmp: Path, timeout: float, argv: list[str]) -> subprocess.CompletedProcess:
    frame = pods.headless_frame(timeout, {"CLAUDE_CONFIG_DIR": str(tmp / "cfg")}, argv)
    return _run(["sh", "-c", pods.HEADLESS_SCRIPT, "_", str(tmp), "3"], tmp, frame)


def test_headless_script_returns_exit_status(tmp_path):
    assert _headless(tmp_path, 30, ["sh", "-c", "exit 3"]).returncode == 3


def test_headless_script_timeout_returns_124(tmp_path):
    assert _headless(tmp_path, 1, ["sleep", "5"]).returncode == 124


def test_scripts_pin_setsid_wait():
    for script in (pods._EXEC_OUTER, pods.HEADLESS_SCRIPT):
        assert "exec setsid --wait sh -c" in script
        assert "exec setsid sh" not in script
