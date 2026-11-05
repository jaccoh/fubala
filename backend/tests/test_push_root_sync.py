"""Push delivery: keep the project root current with the remote default branch.

Before each spec run, worktree creation and take-in-before-verify, Fubala
fetches `origin` and fast-forwards the project root to the remote's default
branch — but only when the root is strictly behind. Dirty or diverged roots
are the operator's: warned about, never touched, and the story still runs on
local HEAD.
"""

import contextlib
import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import workspaces
from app.main import create_app
from conftest import MINIMAL_SPEC


def init_repo(tmp_path) -> str:
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return str(repo)


def init_remote(tmp_path, repo: str) -> str:
    """A bare repo wired up as `origin` of the working repo."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "-C", repo, "remote", "add", "origin", str(bare)], check=True)
    subprocess.run(
        ["git", "-C", repo, "push", "-q", "origin", "HEAD:refs/heads/main"], check=True
    )
    return str(bare)


def _git_out(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: str, msg: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg],
        cwd=repo,
        check=True,
    )


class ScriptedGW:
    """spec -> ai review approve -> coder write -> code review approve.

    On the very first (spec) call it records the project root's HEAD, so a
    test can prove the root was already fast-forwarded before the spec ran.
    """

    def __init__(self, root_to_watch: str | None = None):
        self.calls = 0
        self.root_to_watch = root_to_watch
        self.head_at_spec: str | None = None

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
        from app.providers import Delta as D
        from app.providers import ToolCall as TC
        from app.providers import Usage as U

        self.calls += 1
        if self.calls == 1:  # spec
            if self.root_to_watch:
                self.head_at_spec = _git_out(self.root_to_watch, "rev-parse", "HEAD")
            yield D(text=MINIMAL_SPEC)
        elif self.calls == 2:  # ai review of spec
            yield D(text="fine\n\nVERDICT: APPROVED")
        elif self.calls == 3:  # coder: one write
            yield TC(
                id="w1",
                name="write_file",
                arguments=json.dumps(
                    {"path": "feature.py", "content": "VALUE = 'pushed'\n"}
                ),
            )
        elif self.calls == 4:  # coder final text
            yield D(text="implemented feature.py")
        else:  # code review
            yield D(text="diff matches spec\n\nVERDICT: APPROVED")
        yield U(prompt_tokens=1, completion_tokens=1)


def wait_status(client, sid, want, timeout=30.0):
    deadline = time.time() + timeout
    cur = None
    while time.time() < deadline:
        cur = client.get(f"/api/v1/stories/{sid}").json()
        if cur["status"] in want:
            return cur
        time.sleep(0.05)
    pytest.fail(
        f"never reached {want}; last: {cur and cur['status']} {cur and cur.get('error')}"
    )


def run_to_human_review(client, sid) -> dict:
    """queue -> spec gate -> (human approves the spec) -> coder -> code gate.

    AC1 needs the worktree, which only exists once the coder ran, so the
    helper walks the story all the way to the SECOND human review (the code
    gate) — the same two-gate walk as test_delivery_push.py.
    """
    client.post(f"/api/v1/stories/{sid}/queue")
    current = wait_status(client, sid, ("human_review", "failed"))
    assert current["status"] == "human_review", current.get("error")
    client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
    current = wait_status(client, sid, ("human_review", "failed", "done"))
    assert current["status"] == "human_review", current.get("error")
    return current


def note_events(detail: dict) -> list[dict]:
    return [
        json.loads(e.get("data") or "{}")
        for e in detail.get("events", [])
        if e.get("kind") == "note"
    ]


def sync_warnings(detail: dict) -> list[dict]:
    """push-sync warning notes: authored by fubala, message prefix pinned."""
    out = []
    for n in note_events(detail):
        warning = n.get("warning")
        if isinstance(warning, str) and warning.startswith("push-sync:"):
            out.append(n)
    return out


def _board(tmp_path, monkeypatch, repo: str, gw, delivery: str = "push"):
    """(client, story_id) for a one-project board bound to `repo`.

    self_update stays OFF: merge-mode approve restarts the server through the
    detached self-update helper when the story's root is the global root, and
    that helper waits for THIS process to exit — a test that approves in
    merge mode would wedge until killed. Push mode never restarts, and no
    test here needs the feature.
    """
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "sync.db"))

    @contextlib.contextmanager
    def _ctx():
        with TestClient(create_app(gateway=gw)) as client:
            client.patch(
                "/api/v1/settings",
                json={
                    "story_poll_interval": 0.05,
                    "project_root": repo,
                    "write_tools_enabled": True,
                    "self_update_enabled": False,
                },
            )
            project = client.post(
                "/api/v1/projects",
                json={"name": "p", "root": repo, "delivery": delivery},
            ).json()
            story = client.post(
                "/api/v1/stories",
                json={"title": "feature", "project_id": project["id"]},
            ).json()
            yield client, story["id"]

    return _ctx()


def _revert_on_remote(tmp_path, repo: str, bare: str) -> str:
    """Someone else pushes a commit that deletes `PUSH_RETRY_MAX = 5`.

    The remote advances past the root by exactly that revert, so the root is
    strictly behind and a fast-forward is the only legal move.
    """
    (Path(repo) / "config.py").write_text("PUSH_RETRY_MAX = 5\n")
    _commit(repo, "add PUSH_RETRY_MAX")
    subprocess.run(
        ["git", "-C", repo, "push", "-q", "origin", "HEAD:refs/heads/main"], check=True
    )
    dev = tmp_path / "dev"
    subprocess.run(["git", "clone", "-q", str(bare), str(dev)], check=True)
    (dev / "config.py").write_text("# PUSH_RETRY_MAX reverted upstream\n")
    _commit(str(dev), "revert PUSH_RETRY_MAX")
    subprocess.run(
        ["git", "-C", str(dev), "push", "-q", "origin", "HEAD:refs/heads/main"],
        check=True,
    )
    return _git_out(str(bare), "rev-parse", "refs/heads/main")


# ---- sync_root: unit level -------------------------------------------------


def test_sync_root_unit_outcomes(tmp_path):
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)

    # no remote configured -> no_remote, no fetch attempted
    lone = tmp_path / "lone"
    lone.mkdir()
    subprocess.run(["git", "clone", "-q", str(tmp_path / "remote.git"), str(lone)], check=True)
    subprocess.run(["git", "-C", str(lone), "remote", "remove", "origin"], check=True)
    assert workspaces.sync_root(str(lone)).kind == "no_remote"

    # in sync -> up_to_date
    out = workspaces.sync_root(repo)
    assert out.kind == "up_to_date"
    assert out.ref  # the resolved default ref is reported


def test_sync_root_fast_forwards_a_strictly_behind_root(tmp_path):
    repo = init_repo(tmp_path)
    bare = init_remote(tmp_path, repo)
    tip = _revert_on_remote(tmp_path, repo, bare)

    out = workspaces.sync_root(repo)

    assert out.kind == "ff"
    assert out.old != out.new == tip
    assert _git_out(repo, "rev-parse", "HEAD") == tip
    assert "PUSH_RETRY_MAX = 5" not in (Path(repo) / "config.py").read_text()


def test_sync_root_leaves_ahead_and_dirty_roots_alone(tmp_path):
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)

    # root ahead of the remote (a commit the remote lacks)
    (Path(repo) / "local.txt").write_text("operator's own work\n")
    _commit(repo, "local only")
    head = _git_out(repo, "rev-parse", "HEAD")
    out = workspaces.sync_root(repo)
    assert out.kind == "diverged"
    assert _git_out(repo, "rev-parse", "HEAD") == head

    # root dirty (tracked change, uncommitted)
    (Path(repo) / "README.md").write_text("# dirty\n")
    out = workspaces.sync_root(repo)
    assert out.kind == "dirty"
    assert _git_out(repo, "rev-parse", "HEAD") == head


def test_sync_root_reports_a_failed_fetch(tmp_path):
    repo = init_repo(tmp_path)
    subprocess.run(
        ["git", "-C", repo, "remote", "add", "origin", str(tmp_path / "missing.git")],
        check=True,
    )
    out = workspaces.sync_root(repo)
    assert out.kind == "fetch_failed"


def test_sync_root_untracked_files_do_not_block_the_fast_forward(tmp_path):
    """Gate note: only tracked changes count as dirty — a stray mockup or
    editor file must never turn every story into a warning."""
    repo = init_repo(tmp_path)
    bare = init_remote(tmp_path, repo)
    tip = _revert_on_remote(tmp_path, repo, bare)
    (Path(repo) / "stray-mockup.md").write_text("an untested idea\n")
    (Path(repo) / ".idea").mkdir()
    (Path(repo) / ".idea" / "workspace.xml").write_text("<editor/>\n")

    out = workspaces.sync_root(repo)

    assert out.kind == "ff"
    assert _git_out(repo, "rev-parse", "HEAD") == tip
    # the untracked files survive the fast-forward untouched
    assert (Path(repo) / "stray-mockup.md").exists()


# ---- sync_root: the three runner trigger points -----------------------------


def test_behind_root_is_ffed_before_spec_run_and_worktree_creation(
    tmp_path, monkeypatch
):
    repo = init_repo(tmp_path)
    bare = init_remote(tmp_path, repo)
    tip = _revert_on_remote(tmp_path, repo, bare)

    gw = ScriptedGW(root_to_watch=repo)
    with _board(tmp_path, monkeypatch, repo, gw) as (client, sid):
        run_to_human_review(client, sid)

        # the sync ran BEFORE the spec writer read the root
        assert gw.head_at_spec == tip
        # and before the worktree was forked: the coder's worktree carries the
        # remote's version of the reverted line
        assert _git_out(repo, "rev-parse", "HEAD") == tip
        wt = Path(repo) / ".fubala-worktrees" / sid
        assert wt.exists(), f"worktree missing: {list(wt.parent.glob('*'))}"
        cfg = wt / "config.py"
        assert cfg.exists(), f"config.py missing; worktree holds {list(wt.iterdir())}"
        assert "PUSH_RETRY_MAX = 5" not in cfg.read_text()
        # an in-sync root is a non-event: no warning
        assert sync_warnings(client.get(f"/api/v1/stories/{sid}").json()) == []


def test_diverged_root_is_left_alone_with_a_warning(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    (Path(repo) / "local.txt").write_text("operator's own work\n")
    _commit(repo, "local only")
    head = _git_out(repo, "rev-parse", "HEAD")

    with _board(tmp_path, monkeypatch, repo, ScriptedGW()) as (client, sid):
        run_to_human_review(client, sid)

        assert _git_out(repo, "rev-parse", "HEAD") == head
        detail = client.get(f"/api/v1/stories/{sid}").json()
        warnings = sync_warnings(detail)
        assert warnings, note_events(detail)
        assert warnings[0].get("by") == "fubala"
        assert "diverged" in warnings[0]["warning"]


def test_dirty_root_is_left_alone_with_a_warning(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    (Path(repo) / "README.md").write_text("# operator is mid-edit\n")
    head = _git_out(repo, "rev-parse", "HEAD")

    with _board(tmp_path, monkeypatch, repo, ScriptedGW()) as (client, sid):
        run_to_human_review(client, sid)

        assert _git_out(repo, "rev-parse", "HEAD") == head
        detail = client.get(f"/api/v1/stories/{sid}").json()
        warnings = sync_warnings(detail)
        assert warnings, json.dumps(detail.get("events"))[:3000]
        assert warnings[0].get("by") == "fubala"
        assert "dirty" in warnings[0]["warning"]


def test_unreachable_remote_still_reaches_human_review(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    subprocess.run(
        ["git", "-C", repo, "remote", "add", "origin", str(tmp_path / "missing.git")],
        check=True,
    )
    head = _git_out(repo, "rev-parse", "HEAD")

    with _board(tmp_path, monkeypatch, repo, ScriptedGW()) as (client, sid):
        run_to_human_review(client, sid)

        # the story proceeds on local HEAD, never fails for a remote problem
        assert _git_out(repo, "rev-parse", "HEAD") == head
        detail = client.get(f"/api/v1/stories/{sid}").json()
        warnings = sync_warnings(detail)
        assert warnings, note_events(detail)
        assert warnings[0].get("by") == "fubala"
        assert "fetch" in warnings[0]["warning"]


def test_merge_delivery_project_never_syncs(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    calls = []
    real = workspaces.sync_root

    def spy(project_root, remote="origin"):
        calls.append(project_root)
        return real(project_root, remote)

    monkeypatch.setattr(workspaces, "sync_root", spy)
    with _board(tmp_path, monkeypatch, repo, ScriptedGW(), delivery="merge") as (
        client,
        sid,
    ):
        run_to_human_review(client, sid)
        client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
        final = wait_status(client, sid, ("done", "failed"))
        assert final["status"] == "done", final.get("error")
        assert calls == []  # merge delivery: no fetch, no ff, ever
