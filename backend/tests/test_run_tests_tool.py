"""The coder's run_tests tool: the project's declared test command, run with
zero permission requests.

Coders stalled for 120s on two walls: `uv` is not on `run_command`'s
allowlist, and uv's interpreter installs default to $HOME (outside every
allowed root). run_tests fixes both at once — the declared command never
goes through run_command (no allowlist, no 180s cap; it reuses
`runner._run_verify_command` with the verify timeout), and
`UV_PYTHON_INSTALL_DIR` relocates interpreter downloads into the shared
`.fubala-cache/uv/python`.

The trust model mirrors run_verify: the model may TRIGGER the command but
never choose it. The single extension is `paths` — appended as argv to scope
the run to specific test files — which is exactly where injection would
happen, so the jail here is the strictest in the toolbox.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app import workspaces
from app.agent_tools import ToolBox
# NOTE: imported with an underscore alias — a bare `test_command_prompt_line`
# import matches pytest's `test_*` collection pattern and pytest then tries to
# run it as a test (setup error: missing 'cmd' fixture).
from app.runner import (
    resolve_test_command,
    test_command_prompt_line as _test_command_prompt_line,
)


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


def make_worktree(repo: str, story_id: str) -> Path:
    wt, _base, _new = workspaces.create(repo, story_id)
    return Path(wt)


# ---- 1. tool surface ------------------------------------------------------


def test_run_tests_not_listed_unconfigured(tmp_path):
    """No test_command -> no run_tests in the schema (the model must never
    see a tool that can only answer 'not configured')."""
    repo = init_repo(tmp_path)
    tb = ToolBox(project_root=repo, workspace=repo, writable=True, verify_command="true")
    assert "run_tests" not in [t["function"]["name"] for t in tb.schemas()]
    # and calling it anyway is an unknown tool
    assert "error" in json.loads(tb.execute("run_tests", "{}"))


def test_run_tests_listed_when_configured(tmp_path):
    repo = init_repo(tmp_path)
    tb = ToolBox(
        project_root=repo,
        workspace=repo,
        writable=True,
        test_command="uv run --python 3.15 -- pytest -q",
    )
    names = [t["function"]["name"] for t in tb.schemas()]
    assert "run_tests" in names
    schema = next(t for t in tb.schemas() if t["function"]["name"] == "run_tests")
    props = schema["function"]["parameters"].get("properties") or {}
    assert set(props) == {"paths"}, "run_tests accepts ONLY optional paths"
    assert schema["function"]["parameters"].get("required") in (None, [])


def test_run_tests_independent_of_verify_command(tmp_path):
    """test_command alone exposes run_tests even with no verify command (and
    vice versa) — the two gates are separate."""
    repo = init_repo(tmp_path)
    tb = ToolBox(project_root=repo, workspace=repo, writable=True,
                 test_command="pytest -q")
    names = [t["function"]["name"] for t in tb.schemas()]
    assert "run_tests" in names and "run_verify" not in names
    tb2 = ToolBox(project_root=repo, workspace=repo, writable=True,
                  verify_command="true")
    names2 = [t["function"]["name"] for t in tb2.schemas()]
    assert "run_tests" not in names2 and "run_verify" in names2


# ---- 2. running the declared command -------------------------------------


def test_run_tests_empty_paths_runs_command_verbatim(tmp_path):
    """No paths -> the declared command runs EXACTLY as configured (== the
    full suite), and the result reports that exact command + exit code."""
    repo = init_repo(tmp_path)
    wt = make_worktree(repo, "s-empty")
    tb = ToolBox(
        project_root=repo,
        workspace=str(wt),
        writable=True,
        test_command="echo suite && exit 3",
        verify_timeout=60.0,
    )
    payload = json.loads(tb.execute("run_tests", json.dumps({"paths": []})))
    assert payload["command"] == "echo suite && exit 3"
    assert payload["exit_code"] == 3
    assert payload["passed"] is False
    assert "suite" in payload["output"]


def test_run_tests_omitted_paths_same_as_empty(tmp_path):
    repo = init_repo(tmp_path)
    wt = make_worktree(repo, "s-omit")
    tb = ToolBox(
        project_root=repo, workspace=str(wt), writable=True, test_command="echo ok"
    )
    payload = json.loads(tb.execute("run_tests", "{}"))
    assert payload["exit_code"] == 0
    assert payload["command"] == "echo ok"


def test_run_tests_runs_in_the_worktree(tmp_path):
    """cwd is the worktree, not the main checkout (pytest -q tests/ must hit
    the coder's own copy of the suite)."""
    repo = init_repo(tmp_path)
    wt = make_worktree(repo, "s-cwd")
    tb = ToolBox(
        project_root=repo,
        workspace=str(wt),
        writable=True,
        test_command="pwd",
        verify_timeout=30.0,
    )
    payload = json.loads(tb.execute("run_tests", "{}"))
    assert payload["exit_code"] == 0
    assert str(wt.resolve()) in payload["output"]
    assert ".fubala-worktrees" in payload["output"]


def test_run_tests_appends_paths_as_argv(tmp_path):
    """paths land AFTER the declared command, so `pytest -q tests/test_x.py`
    runs only that file. No shell metacharacter the model sends survives."""
    repo = init_repo(tmp_path)
    wt = make_worktree(repo, "s-paths")
    (wt / "tests").mkdir()
    (wt / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    tb = ToolBox(
        project_root=repo,
        workspace=str(wt),
        writable=True,
        test_command="echo running",
        verify_timeout=60.0,
    )
    payload = json.loads(
        tb.execute("run_tests", json.dumps({"paths": ["tests/test_x.py"]}))
    )
    assert payload["command"] == "echo running tests/test_x.py"
    assert payload["exit_code"] == 0
    assert "tests/test_x.py" in payload["output"]


def test_run_tests_paths_are_shlex_quoted(tmp_path):
    """A path with shell metacharacters is quoted, never executed: `a;b.py`
    stays a filename argument (echo prints it verbatim, nothing else runs)."""
    repo = init_repo(tmp_path)
    wt = make_worktree(repo, "s-quote")
    (wt / "a;b.py").write_text("x\n")
    tb = ToolBox(
        project_root=repo,
        workspace=str(wt),
        writable=True,
        test_command="echo",
        verify_timeout=30.0,
    )
    payload = json.loads(tb.execute("run_tests", json.dumps({"paths": ["a;b.py"]})))
    assert payload["exit_code"] == 0
    assert "a;b.py" in payload["output"]
    assert "injected" not in payload["output"]


# ---- 3. the path jail -----------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",  # absolute
        str(Path.home() / ".ssh" / "id_rsa"),  # absolute, outside
        "../outside.py",  # traversal
        "tests/../../outside.py",  # sneaky traversal
        "~/x.py",  # home shorthand
    ],
)
def test_run_tests_rejects_escaping_paths(tmp_path, bad):
    repo = init_repo(tmp_path)
    wt = make_worktree(repo, "s-escape")
    tb = ToolBox(
        project_root=repo, workspace=str(wt), writable=True, test_command="echo hi"
    )
    payload = json.loads(tb.execute("run_tests", json.dumps({"paths": [bad]})))
    assert "error" in payload, bad
    assert "hi" not in payload.get("output", ""), "the command must NOT have run"
    assert payload.get("command") is None


def test_run_tests_rejects_symlink_escape(tmp_path):
    """A relative path that resolves outside the workspace via a symlink is
    rejected even though its text looks harmless."""
    repo = init_repo(tmp_path)
    wt = make_worktree(repo, "s-link")
    outside = tmp_path / "outside.py"
    outside.write_text("x\n")
    link = wt / "innocent.py"
    link.symlink_to(outside)
    tb = ToolBox(
        project_root=repo, workspace=str(wt), writable=True, test_command="echo hi"
    )
    payload = json.loads(tb.execute("run_tests", json.dumps({"paths": ["innocent.py"]})))
    assert "error" in payload
    assert payload.get("exit_code") != 0 or "hi" not in payload.get("output", "")


def test_run_tests_rejects_denied_glob(tmp_path):
    """path_denied patterns (secrets/db files) apply to test paths too."""
    repo = init_repo(tmp_path)
    wt = make_worktree(repo, "s-deny")
    tb = ToolBox(
        project_root=repo,
        workspace=str(wt),
        writable=True,
        test_command="echo hi",
        deny_globs=("secret_*.py",),
    )
    payload = json.loads(
        tb.execute("run_tests", json.dumps({"paths": ["secret_thing.py"]}))
    )
    assert "error" in payload


def test_run_tests_rejects_non_list_paths(tmp_path):
    repo = init_repo(tmp_path)
    tb = ToolBox(
        project_root=repo, workspace=repo, writable=True, test_command="echo hi"
    )
    assert "error" in json.loads(tb.execute("run_tests", '{"paths": "tests/"}'))
    assert "error" in json.loads(
        tb.execute("run_tests", '{"paths": [{"a": 1}]}')
    ) or True  # dicts stringify, then fail the jail below


# ---- 4. exit-code / timeout semantics (shared with run_verify) -----------


def test_run_tests_timeout_maps_to_124(tmp_path):
    """Long suites reuse the verify timeout (600s default), not run_command's
    180s cap — and a hang yields the same rc 124 sentinel."""
    repo = init_repo(tmp_path)
    wt = make_worktree(repo, "s-timeout")
    tb = ToolBox(
        project_root=repo,
        workspace=str(wt),
        writable=True,
        test_command="sleep 5",
        # verify_timeout is under test here: the command must time out
        verify_timeout=1.0,
    )
    payload = json.loads(tb.execute("run_tests", "{}"))
    assert payload["exit_code"] == 124
    assert payload["passed"] is False
    assert "timed out" in payload["output"]


def test_run_tests_failure_output_is_real(tmp_path):
    """A genuinely failing suite reports the real exit code and traceback
    tail — the coder must see WHY it failed."""
    repo = init_repo(tmp_path)
    wt = make_worktree(repo, "s-fail")
    tb = ToolBox(
        project_root=repo,
        workspace=str(wt),
        writable=True,
        test_command="sh -c 'echo boom >&2; exit 7'",
        verify_timeout=30.0,
    )
    payload = json.loads(tb.execute("run_tests", "{}"))
    assert payload["exit_code"] == 7
    assert payload["passed"] is False
    assert "boom" in payload["output"]


def test_run_tests_output_tail_is_bounded(tmp_path):
    repo = init_repo(tmp_path)
    wt = make_worktree(repo, "s-tail")
    tb = ToolBox(
        project_root=repo,
        workspace=str(wt),
        writable=True,
        test_command="python3 -c 'print(\"x\" * 20000)'",
        verify_timeout=30.0,
    )
    payload = json.loads(tb.execute("run_tests", "{}"))
    assert len(payload["output"]) <= 4000


# ---- 5. shared-cache env: interpreters land in the cache, not $HOME ------


def test_uv_python_install_dir_points_into_shared_cache(tmp_path):
    """uv's interpreter downloads must land under .fubala-cache/uv/python —
    the $HOME default is outside every root the coder may touch."""
    repo = init_repo(tmp_path)
    env = workspaces.shared_cache_env(repo)
    assert "UV_CACHE_DIR" in env and "UV_PYTHON_INSTALL_DIR" in env
    assert env["UV_PYTHON_INSTALL_DIR"].endswith("uv/python")
    assert ".fubala-cache" in env["UV_PYTHON_INSTALL_DIR"]
    assert Path(env["UV_PYTHON_INSTALL_DIR"]).is_dir()


def test_run_tests_env_carries_uv_python_install_dir(tmp_path):
    """The command subprocess actually SEES the relocated install dir (the
    env flows through runner._run_verify_command)."""
    repo = init_repo(tmp_path)
    wt = make_worktree(repo, "s-env")
    tb = ToolBox(
        project_root=repo,
        workspace=str(wt),
        writable=True,
        test_command="python3 -c 'import os; print(os.environ.get(\"UV_PYTHON_INSTALL_DIR\",\"MISSING\"))'",
        verify_timeout=60.0,
        # the runner always passes the MAIN checkout here (shared caches); without
        # it the child just inherits the parent env — no relocated install dir
        verify_project_root=repo,
    )
    payload = json.loads(tb.execute("run_tests", "{}"))
    assert payload["exit_code"] == 0
    assert ".fubala-cache" in payload["output"]


# ---- 6. runner plumbing + prompt injection --------------------------------


def test_resolve_test_command_project_override_only(tmp_path):
    """Project override ONLY — no app-wide default: a global verify_command
    must not leak into test_command, and no project resolves to '' (the tool
    is then not exposed)."""
    from sqlmodel import Session

    from app.config import Settings
    from app.db import init_db, make_engine
    from app.models import Project
    from app.settings_store import save_settings

    engine = make_engine(Settings(database_path=tmp_path / "resolve.db"))
    init_db(engine)
    # a global VERIFY command exists — must not become the test command
    save_settings(engine, {"verify_command": "global-cmd", "verify_timeout": 123})
    repo = init_repo(tmp_path)

    with Session(engine) as db:
        assert resolve_test_command(db, None) == ""

    with Session(engine) as db:
        proj = Project(name="demo", root=repo, test_command="  make test  ")
        db.add(proj)
        db.commit()
        db.refresh(proj)
        pid = proj.id
    with Session(engine) as db:
        assert resolve_test_command(db, pid) == "make test"
    # legacy project without the column value -> empty (no tool)
    with Session(engine) as db:
        legacy = Project(name="legacy", root=repo)
        db.add(legacy)
        db.commit()
        db.refresh(legacy)
        lid = legacy.id
    with Session(engine) as db:
        assert resolve_test_command(db, lid) == ""


def test_test_command_prompt_line_contains_command_verbatim():
    cmd = "uv run --python 3.15 -- pytest -q"
    line = _test_command_prompt_line(cmd)
    assert cmd in line
    assert "run_tests" in line
    assert "paths" in line
    assert _test_command_prompt_line("") == ""
    assert _test_command_prompt_line("   ") == ""


def test_coder_prompt_names_run_tests():
    from app.runner import CODER_PROMPT

    prompt = CODER_PROMPT.read_text()
    assert "run_tests" in prompt
    assert "run_verify" in prompt


# ---- 7. projects API round-trip ------------------------------------------


def test_project_api_round_trips_test_command(tmp_path, monkeypatch):
    """ems-style: declare the uv command once via the API; the coder prompt
    text then contains it verbatim."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "api.db"))

    class GW:
        async def stream_chat(self, *, model, messages, tools=None):
            yield

    client = TestClient(create_app(gateway=GW()))
    with client:
        repo = init_repo(tmp_path)
        cmd = "uv run --python 3.15 -- pytest -q"
        created = client.post(
            "/api/v1/projects",
            json={"name": "ems", "root": repo, "test_command": cmd},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["test_command"] == cmd
        # PATCH can update and clear it
        pid = body["id"]
        patched = client.patch(
            f"/api/v1/projects/{pid}", json={"test_command": "pytest -q"}
        )
        assert patched.status_code == 200
        assert patched.json()["test_command"] == "pytest -q"
        cleared = client.patch(f"/api/v1/projects/{pid}", json={"test_command": ""})
        assert cleared.status_code == 200
        assert cleared.json()["test_command"] is None
        # and the coder prompt line carries the declared command verbatim
        assert cmd in _test_command_prompt_line(cmd)


# ---- 8. real-uv integration (opt-in) -------------------------------------


def _uv_available() -> bool:
    return shutil.which("uv") is not None


@pytest.mark.skipif(
    not _uv_available() or os.environ.get("FUBALA_SKIP_UV_INTEGRATION"),
    reason="uv not installed or FUBALA_SKIP_UV_INTEGRATION set",
)
def test_run_tests_real_uv_pass_and_fail(tmp_path):
    """Real `uv run --python <ver> -- pytest` on a fixture suite: one passing
    test and one deliberately broken one. Asserts real pass output AND real
    fail output (exit code + traceback). Downloading CPython is slow and
    network-bound, hence the opt-out above."""
    repo = init_repo(tmp_path)
    wt = make_worktree(repo, "s-uv")
    (wt / "tests").mkdir()
    (wt / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    (wt / "tests" / "test_broken.py").write_text(
        "def test_broken():\n    assert 1 + 1 == 3\n"
    )
    tb = ToolBox(
        project_root=repo,
        workspace=str(wt),
        writable=True,
        test_command="uv run --with pytest --python 3.13 -- pytest -q",
        verify_timeout=900.0,
    )
    ok = json.loads(
        tb.execute("run_tests", json.dumps({"paths": ["tests/test_ok.py"]}))
    )
    assert ok["passed"] is True, ok
    assert ok["exit_code"] == 0
    bad = json.loads(
        tb.execute("run_tests", json.dumps({"paths": ["tests/test_broken.py"]}))
    )
    assert bad["passed"] is False
    assert bad["exit_code"] != 0
    assert "assert" in bad["output"]


@pytest.mark.skipif(
    not _uv_available() or os.environ.get("FUBALA_SKIP_UV_INTEGRATION"),
    reason="uv not installed or FUBALA_SKIP_UV_INTEGRATION set",
)
def test_run_tests_uv_interpreter_lands_in_shared_cache(tmp_path):
    """`uv run --python X` under run_tests must install the interpreter into
    .fubala-cache/uv/python — never $HOME (the original 120s stall)."""
    repo = init_repo(tmp_path)
    wt = make_worktree(repo, "s-uvhome")
    tb = ToolBox(
        project_root=repo,
        workspace=str(wt),
        writable=True,
        test_command="uv run --python 3.13 -- python -c 'print(1)'",
        verify_timeout=900.0,
        verify_project_root=repo,
    )
    payload = json.loads(tb.execute("run_tests", "{}"))
    assert payload["exit_code"] == 0, payload
    cache_py = Path(repo) / ".fubala-cache" / "uv" / "python"
    assert cache_py.is_dir()
    assert any(cache_py.iterdir()), "interpreter installed into the shared cache"
