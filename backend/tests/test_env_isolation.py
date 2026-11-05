"""The suite must not depend on the host's git identity (a worker pod has none)."""

import os
import subprocess

from app import workspaces


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def test_merge_without_inline_identity_succeeds_with_an_empty_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    assert not (home / ".gitconfig").exists()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    def commit(name):
        (repo / name).write_text(name)
        _git(repo, "add", name)
        _git(repo, "commit", "-q", "-m", name)

    commit("a")
    _git(repo, "checkout", "-q", "-b", "side")
    commit("b")
    _git(repo, "checkout", "-q", "main")
    commit("c")
    # the same call shape as train.py / workspaces.merge
    workspaces._git(repo, "merge", "--no-ff", "side", "-m", "merge side")
    assert "merge side" in _git(repo, "log", "-1", "--format=%s")


def test_git_config_is_the_suites_own():
    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
    assert os.path.isfile(os.environ["GIT_CONFIG_GLOBAL"])
