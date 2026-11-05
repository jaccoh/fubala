"""docker/entrypoint.sh: git host rewrite, identity and clones come from env."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker" / "entrypoint.sh"


def run_entrypoint(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    base = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "FUBALA_DATA_DIR": str(tmp_path / "data"),
    }
    return subprocess.run(
        ["sh", str(ENTRYPOINT), "true"],
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def git_config(tmp_path: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "config", "--file", str(tmp_path / "gitconfig"), *args],
        capture_output=True,
        text=True,
    )
    return res.stdout


def test_no_public_host_means_no_rewrite(tmp_path):
    res = run_entrypoint(tmp_path, GIT_INTERNAL_URL="http://gitea.internal:3000")
    assert res.returncode == 0, res.stderr
    assert "insteadof" not in git_config(tmp_path, "--list").lower()


def test_internal_url_and_public_host_give_three_rewrites(tmp_path):
    res = run_entrypoint(
        tmp_path, GIT_INTERNAL_URL="http://gitea.internal:3000", GIT_PUBLIC_HOST="git.example.com"
    )
    assert res.returncode == 0, res.stderr
    out = git_config(tmp_path, "--get-all", "url.http://gitea.internal:3000/.insteadOf")
    assert out.split() == [
        "git@git.example.com:",
        "ssh://git@git.example.com/",
        "https://git.example.com/",
    ]


def test_default_identity_email(tmp_path):
    res = run_entrypoint(tmp_path)
    assert res.returncode == 0, res.stderr
    assert git_config(tmp_path, "user.email").strip() == "fubala@localhost"
    assert git_config(tmp_path, "user.name").strip() == "fubala"


def test_clone_goes_through_the_rewrite(tmp_path):
    bare_root = tmp_path / "bare"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    (work / "f.txt").write_text("x")
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.test"]
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), *ident, "commit", "-qm", "init"], check=True)
    (bare_root / "acme").mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(work), str(bare_root / "acme" / "app.git")],
        check=True,
    )
    res = run_entrypoint(
        tmp_path,
        GIT_INTERNAL_URL=f"file://{bare_root}",
        GIT_PUBLIC_HOST="example.test",
        FUBALA_REPOS="app=acme/app",
    )
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "data" / "repos" / "app" / "f.txt").read_text() == "x"


def test_repos_without_public_host_fail(tmp_path):
    res = run_entrypoint(tmp_path, FUBALA_REPOS="app=acme/app")
    assert res.returncode != 0
    assert "GIT_PUBLIC_HOST" in res.stderr
