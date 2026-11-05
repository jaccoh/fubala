"""Warm verify: shared npm/uv caches and `npm ci` skipping in story worktrees.

Two things are under test:
1. `workspaces.shared_cache_env` points npm/uv at repo-level cache dirs and
   hides them from `git status` via the local exclude.
2. The lockfile stamp lets `_run_verify_command` replace a redundant
   `npm ci` with a no-op while leaving the command otherwise verbatim.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.agent_tools import ToolBox
from app.runner import (
    _lock_hash,
    _npm_project_dirs,
    _prepare_verify,
    _run_verify_command,
    _stamp_path,
    _stale,
    _write_stamps,
)
from app.workspaces import shared_cache_env


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(cwd),
        },
    )


def _git_out(*args: str, cwd: Path) -> str:
    """Same as _git but returns stdout, for rev-parse style queries."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(cwd),
        },
    ).stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# repo\n")
    _git("init", "-q", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)
    return repo


# --- shared cache env ------------------------------------------------------


def test_verify_command_sees_shared_cache_dirs(tmp_path):
    repo = make_repo(tmp_path)
    ws = repo / ".fubala-worktrees" / "s1"
    ws.mkdir(parents=True)

    rc, output = _run_verify_command(
        ws, "echo $npm_config_cache $UV_CACHE_DIR", 30, str(repo)
    )

    assert rc == 0
    npm_dir, uv_dir = output.split()
    assert Path(npm_dir).is_dir() and Path(uv_dir).is_dir()
    assert npm_dir.endswith(".fubala-cache/npm")
    assert uv_dir.endswith(".fubala-cache/uv")
    assert Path(npm_dir).parent.parent == repo
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert ".fubala-cache/" in exclude
    # the cache dir never shows up as untracked noise
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert ".fubala-cache" not in status


def test_coder_run_command_gets_shared_cache(tmp_path):
    repo = make_repo(tmp_path)
    box = ToolBox(
        project_root=str(repo),
        workspace=str(repo),
        allow_commands=("printenv",),
        writable=True,
    )
    result = box._run_command({"command": "printenv npm_config_cache"})
    cache = repo / ".fubala-cache" / "npm"
    assert result.strip().endswith(str(cache))
    assert cache.is_dir()


def test_shared_cache_env_survives_bad_project_root(tmp_path):
    """Caching is best-effort: a bogus root yields an empty env, never an
    exception that would fail a verify run."""
    assert shared_cache_env(str(tmp_path / "nope")) == {}


def test_cache_dir_excluded_when_project_root_is_a_worktree(tmp_path):
    """A story worktree's `.git` is a FILE, so `repo/.git/info/exclude` is
    "not a directory" there. The exclude must land in the real git dir
    (shared by every worktree of the repo) or `.fubala-cache/` leaks into
    `git status` for every later story."""
    repo = make_repo(tmp_path)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "-b", "wt", str(wt), cwd=repo)
    assert (wt / ".git").is_file()  # the linked-worktree shape

    env = shared_cache_env(str(wt))

    common = _git_out("rev-parse", "--git-common-dir", cwd=repo)
    git_dir = Path(common) if common.startswith("/") else repo / common
    exclude = (git_dir / "info" / "exclude").read_text()
    assert ".fubala-cache/" in exclude
    # the cache resolved to the repo toplevel, not the worktree. Compared as
    # resolved paths: on macOS tmp_path is /var/... while git reports the
    # /private/var/... form of the very same directory.
    repo_real = repo.resolve()
    assert Path(env["npm_config_cache"]).resolve().is_relative_to(repo_real)
    assert Path(env["UV_CACHE_DIR"]).resolve().is_relative_to(repo_real)
    assert not Path(env["npm_config_cache"]).is_relative_to(wt)
    # and no stray .git directory got created inside the worktree
    assert not (wt / ".git" / "info").exists()


# --- lockfile stamps -------------------------------------------------------


def make_pkg(root: Path) -> Path:
    pkg = root / "frontend"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "package.json").write_text('{"name": "frontend"}\n')
    (pkg / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    (pkg / "node_modules").mkdir(exist_ok=True)
    return pkg


def test_stamp_roundtrip_and_skip(tmp_path):
    repo = make_repo(tmp_path)
    ws = repo / ".fubala-worktrees" / "s2"
    ws.mkdir(parents=True)
    pkg = make_pkg(ws)

    # nothing stamped yet -> stale, command untouched
    assert _stale(pkg) is True
    cmd, note = _prepare_verify(ws, "npm ci --prefix frontend && npm test")
    assert cmd == "npm ci --prefix frontend && npm test"
    assert note == ""

    _write_stamps(ws)
    stamp = _stamp_path(pkg)
    assert stamp.is_file()
    assert stamp.read_text().strip() == _lock_hash(pkg / "package-lock.json")
    assert _stale(pkg) is False

    # word-bounded rewrite keeps flags and the rest of the chain intact
    cmd, note = _prepare_verify(ws, "npm ci --prefix frontend && npm test")
    assert cmd == ": --prefix frontend && npm test"
    assert "npm ci skipped" in note

    # a changed lockfile invalidates the stamp again
    (pkg / "package-lock.json").write_text('{"lockfileVersion": 3, "x": 1}\n')
    assert _stale(pkg) is True
    cmd, _ = _prepare_verify(ws, "npm ci && npm test")
    assert cmd == "npm ci && npm test"


def test_stamp_missing_or_modules_gone(tmp_path):
    repo = make_repo(tmp_path)
    ws = repo / ".fubala-worktrees" / "s3"
    ws.mkdir(parents=True)
    pkg = make_pkg(ws)
    _write_stamps(ws)

    # missing stamp -> stale
    _stamp_path(pkg).unlink()
    assert _stale(pkg) is True

    # node_modules gone -> stale
    _write_stamps(ws)
    assert _stale(pkg) is False
    import shutil

    shutil.rmtree(pkg / "node_modules")
    assert _stale(pkg) is True


def test_run_verify_writes_stamp_after_green_round(tmp_path):
    repo = make_repo(tmp_path)
    ws = repo / ".fubala-worktrees" / "s4"
    ws.mkdir(parents=True)
    pkg = make_pkg(ws)

    rc, output = _run_verify_command(ws, "true", 30, str(repo))
    assert rc == 0
    assert "skipped" not in output
    assert _stamp_path(pkg).is_file()

    # second round: lockfile unchanged -> npm ci is replaced and noted
    rc, output = _run_verify_command(ws, "npm ci && true", 30, str(repo))
    assert rc == 0
    assert "npm ci skipped (lockfile unchanged)" in output


def test_npm_project_dirs_only_counts_immediate_children(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text("{}\n")
    (ws / "package-lock.json").write_text("{}\n")
    child = ws / "frontend"
    child.mkdir()
    (child / "package.json").write_text("{}\n")
    (child / "package-lock.json").write_text("{}\n")
    deep = ws / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "package.json").write_text("{}\n")
    (deep / "package-lock.json").write_text("{}\n")  # too deep to notice
    nodir = ws / "docs"
    nodir.mkdir()
    (nodir / "package.json").write_text("{}\n")  # not a lockfile
    dirs = {d.name for d in _npm_project_dirs(ws)}
    assert dirs == {"ws", "frontend"}


def test_lockfile_without_package_json_is_not_a_project(tmp_path):
    """A stray committed lockfile (no package.json beside it) must not read
    as a forever-stale npm project — that would disable skipping worktree-wide
    and, worse, rewrite a `npm ci` whose real behaviour is to fail."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package-lock.json").write_text("{}\n")
    assert _npm_project_dirs(ws) == []
    cmd, note = _prepare_verify(ws, "npm ci && npm test")
    assert cmd == "npm ci && npm test" and note == ""
