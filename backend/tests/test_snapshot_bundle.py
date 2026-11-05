"""workspaces.snapshot_bundle: ship uncommitted and untracked work without
touching HEAD, the index, the files or the refs of the source worktree."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from app import workspaces


def git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )  # fmt: skip
    return res.stdout


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "src"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("one\n")
    (repo / ".gitignore").write_text("ignored/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


def state(repo: Path) -> tuple:
    index = Path(git(repo, "rev-parse", "--absolute-git-dir").strip()) / "index"
    return (
        git(repo, "rev-parse", "HEAD"),
        hashlib.sha256(index.read_bytes()).hexdigest(),
        git(repo, "status", "--porcelain"),
        git(repo, "for-each-ref"),
    )


def receive(dest: Path, bundle: bytes, sha: str, ref: str) -> None:
    """What the pod does with the bundle."""
    if not (dest / ".git").exists():
        dest.mkdir(exist_ok=True)
        git(dest, "init", "-q")
    if bundle:
        f = dest.parent / "b.bundle"
        f.write_bytes(bundle)
        git(dest, "bundle", "verify", str(f))
        git(dest, "fetch", "-q", "--no-tags", str(f), f"+{ref}:refs/fubala/snap")
    git(dest, "checkout", "-q", "--force", "--detach", sha)
    git(dest, "clean", "-fdq")


def test_bundle_carries_uncommitted_and_untracked_and_leaves_source_alone(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "a.txt").write_text("one\ntwo\n")  # unstaged edit
    (repo / "new.txt").write_text("untracked\n")
    (repo / "staged.txt").write_text("staged\n")
    git(repo, "add", "staged.txt")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "x").write_text("secret\n")
    before = state(repo)

    bundle, sha = workspaces.snapshot_bundle(repo, "story1")

    assert state(repo) == before  # HEAD, index, status and refs untouched
    assert (repo / "new.txt").read_text() == "untracked\n"
    dest = tmp_path / "dest"
    receive(dest, bundle, sha, f"{workspaces.SNAPSHOT_REF_PREFIX}story1")
    assert (dest / "a.txt").read_text() == "one\ntwo\n"
    assert (dest / "new.txt").read_text() == "untracked\n"
    assert (dest / "staged.txt").read_text() == "staged\n"
    assert not (dest / "ignored").exists()


def test_incremental_bundle_and_nothing_new(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "new.txt").write_text("v1\n")
    full, sha1 = workspaces.snapshot_bundle(repo, "s")
    dest = tmp_path / "dest"
    ref = f"{workspaces.SNAPSHOT_REF_PREFIX}s"
    receive(dest, full, sha1, ref)

    (repo / "new.txt").write_text("v2\n")
    inc, sha2 = workspaces.snapshot_bundle(repo, "s", since=sha1)
    assert sha2 != sha1 and inc
    receive(dest, inc, sha2, ref)
    assert (dest / "new.txt").read_text() == "v2\n"

    same, sha3 = workspaces.snapshot_bundle(repo, "s", since=sha2)
    assert sha3 == sha2 and same == b""
    receive(dest, same, sha3, ref)  # re-checkout of the same sha is fine
    assert (dest / "new.txt").read_text() == "v2\n"
    assert "fubala-snapshot" not in git(repo, "for-each-ref")
