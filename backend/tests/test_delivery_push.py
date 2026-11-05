"""Delivery mode "push": approving pushes the story branch to the remote
(and optionally opens a Gitea PR) instead of merging into the root checkout."""

import json
import sqlite3
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class ScriptedGW:
    """spec -> ai review approve -> coder write -> code review approve.

    Routed on the runner's prompt phase markers (conftest STORY_PHASE_MARKERS),
    NOT on a call counter: a story that passes the same stage twice (the
    reject -> coder-rerun in the re-approval test) would otherwise exhaust the
    script and make the coder answer with code-review text — "coder produced no
    changes after retries" — regardless of what the delivery code under test
    does."""

    def __init__(self):
        self.calls = 0

    @staticmethod
    def _last_user_text(messages) -> str:
        for message in reversed(messages):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
        from app.providers import Delta as D
        from app.providers import ToolCall as TC
        from app.providers import Usage as U

        self.calls += 1
        prompt = self._last_user_text(messages)
        # a coder continuation round keeps the CODE REQUEST prompt as the last
        # "user" message (the tool result rides in role "tool"), so the
        # follow-up is told apart by that tool message, not by the prompt
        after_tool_round = any(m.get("role") == "tool" for m in messages)
        if "=== SPEC REQUEST ===" in prompt:  # spec
            yield D(text=MINIMAL_SPEC)
        elif "=== AI REVIEW REQUEST ===" in prompt:  # ai review of spec
            yield D(text="fine\n\nVERDICT: APPROVED")
        elif "=== CODE REQUEST ===" in prompt and not after_tool_round:
            # coder, first round: one write
            yield TC(
                id="w1",
                name="write_file",
                arguments=json.dumps(
                    {"path": "feature.py", "content": "VALUE = 'pushed'\n"}
                ),
            )
        elif "=== CODE REQUEST ===" in prompt:
            # coder, follow-up round after the tool result: final text
            yield D(text="implemented feature.py")
        else:  # code review (verdict-bearing text, no tools)
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
    pytest.fail(f"never reached {want}; last: {cur and cur['status']} {cur and cur.get('error')}")


def run_to_human_review(client, sid) -> dict:
    client.post(f"/api/v1/stories/{sid}/queue")
    current = wait_status(client, sid, ("human_review", "failed"))
    assert current["status"] == "human_review", current.get("error")
    client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
    return wait_status(client, sid, ("human_review", "failed", "done"))


def note_events(detail: dict) -> list[dict]:
    return [
        json.loads(e.get("data") or "{}")
        for e in detail.get("events", [])
        if e.get("kind") == "note"
    ]


# ---- workspaces.push -------------------------------------------------------


def test_push_pushes_branch_and_leaves_root_untouched(tmp_path):
    repo = init_repo(tmp_path)
    bare = init_remote(tmp_path, repo)
    wt, _base, _new = workspaces.create(repo, "story1")
    (wt / "feature.py").write_text("VALUE = 'pushed'\n")
    head_before = _git_out(repo, "rev-parse", "HEAD")

    sha = workspaces.push(repo, "story1")

    assert sha == _git_out(repo, "rev-parse", "fubala/story1")
    # the remote now owns the branch with the work on it
    assert _git_out(bare, "rev-parse", "--verify", "refs/heads/fubala/story1") == sha
    assert "VALUE = 'pushed'" in _git_out(bare, "show", "refs/heads/fubala/story1:feature.py")
    # the root checkout is never touched
    assert _git_out(repo, "rev-parse", "HEAD") == head_before
    assert not (Path(repo) / "feature.py").exists()
    assert _git_out(repo, "status", "--porcelain") == ""


def test_push_missing_branch_is_an_error(tmp_path):
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.push(repo, "nope")


def test_push_retries_overwrite_the_remote_ref(tmp_path):
    """A re-run story rewrites its branch; the second push must win."""
    repo = init_repo(tmp_path)
    bare = init_remote(tmp_path, repo)
    wt, _base, _new = workspaces.create(repo, "story1")
    (wt / "feature.py").write_text("VALUE = 'v1'\n")
    first = workspaces.push(repo, "story1")
    # simulate a retry: rewrite the branch tip (reset to base, new commit)
    base = _git_out(repo, "rev-parse", "main")
    subprocess.run(["git", "-C", str(wt), "reset", "-q", "--hard", base], check=True)
    (wt / "feature.py").write_text("VALUE = 'v2'\n")
    subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(wt),
         "commit", "-qm", "v2"], check=True,
    )
    second = workspaces.push(repo, "story1")
    assert second != first
    assert _git_out(bare, "rev-parse", "refs/heads/fubala/story1") == second
    assert "VALUE = 'v2'" in _git_out(bare, "show", "refs/heads/fubala/story1:feature.py")


# ---- project API -----------------------------------------------------------


def test_project_delivery_validation_and_fields(tmp_path, client):
    repo = init_repo(tmp_path)
    res = client.post(
        "/api/v1/projects",
        json={"name": "p", "root": repo, "delivery": "bogus"},
    )
    assert res.status_code == 422
    assert "delivery must be one of ['merge', 'push']" in res.text

    created = client.post(
        "/api/v1/projects", json={"name": "p", "root": repo, "delivery": "push"}
    ).json()
    assert created["delivery"] == "push"
    for field in ("pr_api_base", "pr_repo", "pr_base_branch", "pr_token_file"):
        assert field in created

    patched = client.patch(
        f"/api/v1/projects/{created['id']}", json={"delivery": "nope"}
    )
    assert patched.status_code == 400
    assert "delivery must be one of ['merge', 'push']" in patched.json()["detail"]

    ok = client.patch(
        f"/api/v1/projects/{created['id']}",
        json={
            "delivery": "merge",
            "pr_api_base": "https://git.example.com",
            "pr_repo": "acme/app",
            "pr_base_branch": "main",
            "pr_token_file": "/tmp/token.txt",
        },
    ).json()
    assert ok["delivery"] == "merge"
    assert ok["pr_repo"] == "acme/app"
    assert ok["pr_base_branch"] == "main"
    assert ok["pr_token_file"] == "/tmp/token.txt"


def test_default_delivery_is_merge(tmp_path, client):
    repo = init_repo(tmp_path)
    project = client.post("/api/v1/projects", json={"name": "p", "root": repo}).json()
    assert project["delivery"] == "merge"


def test_migration_backfills_delivery_for_legacy_rows(tmp_path, monkeypatch):
    """A legacy row (delivery IS NULL in the DB) is stamped to 'merge' on boot.

    The assertion at the DB level is the mutation-sensitive one: the read
    path in routers/projects.py coerces a NULL delivery to 'merge' via
    `project.delivery or ProjectDelivery.MERGE`, so an API-only check would
    pass even with the startup UPDATE in main._migrate removed."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
    # first boot creates the schema; put a real project in it via the API
    with TestClient(create_app(gateway=object())) as client:
        created = client.post(
            "/api/v1/projects", json={"name": "legacy", "root": init_repo(tmp_path)}
        ).json()
        assert created["delivery"] == "merge"  # sanity precondition
        pid = created["id"]
    # then simulate the legacy row: wipe delivery back to NULL in the DB
    conn = sqlite3.connect(db_path)
    cur = conn.execute("UPDATE projects SET delivery=NULL WHERE id=:id", {"id": pid})
    assert cur.rowcount == 1
    conn.commit()
    conn.close()
    # restart: the startup backfill must stamp the NULL row to 'merge'
    with TestClient(create_app(gateway=object())) as client:
        projects = client.get("/api/v1/projects").json()
        # mutation-sensitive: query the raw column, not the coerced read path
        check = sqlite3.connect(db_path)
        row = check.execute(
            "SELECT delivery FROM projects WHERE id=:id", {"id": pid}
        ).fetchone()
        check.close()
        assert row is not None
        assert row[0] == "merge"
    assert len(projects) == 1
    assert projects[0]["id"] == pid
    assert projects[0]["delivery"] == "merge"


# ---- approve: push mode ----------------------------------------------------


def test_approve_in_push_mode_pushes_instead_of_merging(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    bare = init_remote(tmp_path, repo)
    head_before = _git_out(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "push.db"))

    with TestClient(create_app(gateway=ScriptedGW())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "self_update_enabled": True,
            },
        )
        project = client.post(
            "/api/v1/projects", json={"name": "p", "root": repo, "delivery": "push"}
        ).json()
        story = client.post(
            "/api/v1/stories",
            json={"title": "feature", "project_id": project["id"]},
        ).json()
        sid = story["id"]
        current = run_to_human_review(client, sid)
        assert current["status"] == "human_review", current.get("error")

        final = client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"}).json()
        assert final["status"] == "done"

        # the branch (with the work) lives on the remote
        sha = _git_out(bare, "rev-parse", "--verify", f"refs/heads/fubala/{sid}")
        assert "VALUE = 'pushed'" in _git_out(bare, "show", f"refs/heads/fubala/{sid}:feature.py")

        # evidence is a NOTE event, never Story.evidence
        detail = client.get(f"/api/v1/stories/{sid}").json()
        assert detail.get("evidence") is None
        notes = note_events(detail)
        delivery_notes = [n for n in notes if n.get("delivery") == "push"]
        assert delivery_notes, notes
        assert delivery_notes[-1].get("pushed_sha") == sha
        assert delivery_notes[-1].get("pr_url") is None
        # the status event says pushed, not merged
        status_events = [
            json.loads(e.get("data") or "{}")
            for e in detail.get("events", [])
            if e.get("kind") == "status"
        ]
        assert status_events[-1].get("pushed") is True
        assert "merged" not in status_events[-1]

        # root checkout untouched, worktree cleaned up like merge mode
        assert _git_out(repo, "rev-parse", "HEAD") == head_before
        assert not (Path(repo) / "feature.py").exists()
        assert not (Path(repo) / ".fubala-worktrees" / sid).exists()


def _push_only_project(client, repo: str) -> dict:
    """A push-delivery project with no pr_* wiring (push-only landing)."""
    return client.post(
        "/api/v1/projects", json={"name": "p", "root": repo, "delivery": "push"}
    ).json()


def test_approve_lands_the_off_branch_coder_commit(tmp_path, monkeypatch):
    """The incident: the coder committed on ext/... — the push must carry the
    worktree HEAD (the reviewed work), never the stale fubala/<id> tip that
    still points at the fork point."""
    repo = init_repo(tmp_path)
    bare = init_remote(tmp_path, repo)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "offbranch-push.db"))

    with TestClient(create_app(gateway=ScriptedGW())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
            },
        )
        project = _push_only_project(client, repo)
        story = client.post(
            "/api/v1/stories",
            json={"title": "feature", "project_id": project["id"]},
        ).json()
        sid = story["id"]
        current = run_to_human_review(client, sid)
        assert current["status"] == "human_review", current.get("error")

        # the incident shape, replayed at the code gate: the worktree moves
        # onto its own branch and further story work is committed THERE —
        # fubala/<id> stays behind at the commit the old push would have
        # shipped (not the coder's current HEAD)
        wt = Path(repo) / ".fubala-worktrees" / sid
        stale_tip = _git_out(repo, "rev-parse", f"fubala/{sid}")
        (wt / "feature.py").write_text("VALUE = 'off-branch'\n")
        subprocess.run(
            ["git", "-C", str(wt), "checkout", "-q", "-b", "ext/feature"], check=True
        )
        subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(wt),
             "commit", "-qm", "off-branch work"],
            check=True,
        )
        coder_sha = _git_out(str(wt), "rev-parse", "HEAD")
        assert coder_sha != stale_tip
        assert _git_out(repo, "rev-parse", f"fubala/{sid}") == stale_tip

        res = client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "done"

        # the remote branch carries the coder's commit — never the fork point
        assert _git_out(bare, "rev-parse", "--verify", f"refs/heads/fubala/{sid}") == (
            coder_sha
        )
        assert "off-branch" in _git_out(
            bare, "show", f"refs/heads/fubala/{sid}:feature.py"
        )
        detail = client.get(f"/api/v1/stories/{sid}").json()
        notes = note_events(detail)
        push_notes = [n for n in notes if n.get("delivery") == "push" and n.get("pushed_sha")]
        assert push_notes, notes
        assert push_notes[-1]["pushed_sha"] == coder_sha
        # the adoption (story branch moved onto the worktree HEAD) is noted
        assert any(n.get("adopted_worktree_head") for n in notes), notes


def test_approve_refuses_an_empty_push(tmp_path, monkeypatch):
    """Branch tip == the pinned fork point, no changes: the approve FAILS the
    story instead of pushing a branch that lands nothing."""
    repo = init_repo(tmp_path)
    bare = init_remote(tmp_path, repo)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "empty-push.db"))

    with TestClient(create_app(gateway=ScriptedGW())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
            },
        )
        project = _push_only_project(client, repo)
        story = client.post(
            "/api/v1/stories",
            json={"title": "feature", "project_id": project["id"]},
        ).json()
        sid = story["id"]
        current = run_to_human_review(client, sid)
        assert current["status"] == "human_review", current.get("error")

        # wipe the coder's work: HEAD and the branch tip are the fork point
        wt = Path(repo) / ".fubala-worktrees" / sid
        base = _git_out(repo, "rev-parse", "HEAD")
        subprocess.run(["git", "-C", str(wt), "reset", "-q", "--hard", base], check=True)
        subprocess.run(["git", "-C", str(wt), "clean", "-qfd"], check=True)

        res = client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
        assert res.status_code == 409
        assert "push refused" in res.json()["detail"]
        story = client.get(f"/api/v1/stories/{sid}").json()
        assert story["status"] == "failed"
        assert "push refused" in story["error"]
        assert "no story work to land" in story["error"]
        # nothing reached the remote, nothing landed, the worktree is kept
        probe = subprocess.run(
            ["git", "-C", bare, "rev-parse", "--verify", f"refs/heads/fubala/{sid}"],
            capture_output=True,
            text=True,
        )
        assert probe.returncode != 0, probe.stdout
        assert wt.exists()


class PausingGW(ScriptedGW):
    """Parks the coder's continuation round until the test releases it.

    The seam for acting on the worktree BETWEEN the coder's write and the
    commit/code review: deterministic (the write tool has run — the tool
    message is in the history — and nothing else touches the worktree while
    the round is parked), no wall-clock race."""

    def __init__(self):
        super().__init__()
        self.wrote = threading.Event()
        self.release = threading.Event()

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
        prompt = self._last_user_text(messages)
        after_tool_round = any(m.get("role") == "tool" for m in messages)
        if "=== CODE REQUEST ===" in prompt and after_tool_round:
            self.wrote.set()
            assert self.release.wait(timeout=30), "test never released the coder"
        async for event in super().stream_chat(
            model=model,
            messages=messages,
            tools=tools,
            max_output_tokens=max_output_tokens,
        ):
            yield event


def test_code_review_flags_a_worktree_off_the_story_branch(tmp_path, monkeypatch):
    """Worktree HEAD on another branch at code review: a
    branch_off_story_branch note exists (with the ref and HEAD sha) and the
    review moves nothing — the flag is advisory, the branch stays as-is."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "offbranch-gate.db"))

    gw = PausingGW()
    with TestClient(create_app(gateway=gw)) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
            },
        )
        project = _push_only_project(client, repo)
        story = client.post(
            "/api/v1/stories",
            json={"title": "feature", "project_id": project["id"]},
        ).json()
        sid = story["id"]
        client.post(f"/api/v1/stories/{sid}/queue")
        current = wait_status(client, sid, ("human_review", "failed"))
        assert current["status"] == "human_review", current.get("error")
        client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})  # spec gate

        # the coder's write tool has run; park the continuation round and
        # move the worktree onto a foreign branch before the commit lands
        assert gw.wrote.wait(timeout=30)
        wt = Path(repo) / ".fubala-worktrees" / sid
        assert (wt / "feature.py").exists()
        fork = _git_out(str(wt), "rev-parse", "HEAD")
        subprocess.run(
            ["git", "-C", str(wt), "checkout", "-q", "-b", "ext/feature"], check=True
        )
        gw.release.set()

        current = wait_status(client, sid, ("human_review", "failed"))
        assert current["status"] == "human_review", current.get("error")
        detail = client.get(f"/api/v1/stories/{sid}").json()
        notes = note_events(detail)
        flagged = [n for n in notes if n.get("branch_off_story_branch")]
        assert flagged, notes
        assert flagged[-1]["head_branch"] == "ext/feature"
        assert flagged[-1]["head_sha"]
        assert flagged[-1]["head_sha"] != fork  # the coder's commit is on ext/*
        # advisory only: the review moved neither the checkout nor the branch
        assert _git_out(str(wt), "rev-parse", "--abbrev-ref", "HEAD") == "ext/feature"
        assert _git_out(repo, "rev-parse", f"fubala/{sid}") == fork


def test_bundled_prompts_forbid_branch_instructions():
    """The bundled defaults carry the no-branch rules (per-app overrides come
    from settings; these ship with every install)."""
    from app.settings_store import PROMPTS_DIR

    spec_writer = (PROMPTS_DIR / "spec_writer.md").read_text()
    assert "Never instruct the coder to create, checkout, or switch branches" in (
        spec_writer
    )
    coder = (PROMPTS_DIR / "coder.md").read_text()
    assert "Never create, checkout, or switch branches" in coder


class _PRHandler(BaseHTTPRequestHandler):
    """Fake Gitea: PR create on POST, PR state on GET, update on POST …/update.

    State lives on the server instance so a test can flip it between polls:
    `server.pulls[index] = {"merged": bool, "mergeable": bool|None, "state": str}`
    and `server.statuses[sha] = {"status": str, "statuses": [...]}`.
    """

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, body=None):
        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "accept": self.headers.get("Accept"),
                "body": body,
            }
        )

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        body = json.loads(raw) if raw else None
        self._record(body)
        # strip the query string before routing: the update endpoint is
        # called with ?style=merge (recorded above in full, matched here
        # without it — the bug the real Gitea client and the fake shared)
        path = self.path.split("?")[0].rstrip("/")
        if path.endswith("/update"):
            index = int(path.split("/")[-2])
            info = self.server.pulls.get(index)
            if self.server.update_up_to_date > 0:
                # Gitea's misused 500 for "the branch already contains its
                # base" — the exact message text the real Gitea answers and
                # the only string pr.py may match on. Consumes itself so a
                # later 500 of a different shape can be layered on top.
                self.server.update_up_to_date -= 1
                self._json(
                    500,
                    {"message": f"HeadBranch of PR {index} is up to date"},
                )
                return
            if self.server.update_server_errors > 0:
                # a plain 500 that is NOT the up-to-date answer: overload
                # noise — the transient path (retry, then "transient network
                # error") must still apply to it
                self.server.update_server_errors -= 1
                self._json(500, {"message": "internal server error"})
                return
            if info is not None and info.get("merged"):
                # real Gitea refuses to update an already-merged branch:
                # retry-land on a merged PR must see this 422, not a 200
                self._json(422, {"message": "pull request was already merged"})
                return
            if self.server.update_flip_merged > 0:
                # the PR merges between the poller's read and this update
                # POST: answer the 422 AND record the merge, so the re-read
                # the recovery does sees a merged PR. Deterministic — no
                # test-side thread racing the poll.
                self.server.update_flip_merged -= 1
                if info is not None:
                    info["merged"] = True
                self._json(422, {"message": "pull request was already merged"})
                return
            if self.server.update_refusals > 0:
                # a branch update Gitea cannot perform (a conflict the base
                # introduced that cannot be auto-merged): 409, the shape the
                # land-race recovery must park at the gate with
                self.server.update_refusals -= 1
                self._json(
                    409, {"message": "merge conflict: cannot update branch"}
                )
                return
            if index in self.server.pull_heads:
                # a real branch update moves the PR's head commit
                if not self.server.update_keeps_head:
                    self.server.pull_heads[index] = (
                        f"{self.server.pull_heads[index]}-updated"
                    )
            self._json(200, {"message": "branch updated"})
            return
        if "/actions/runs/" in path and path.endswith("/rerun"):
            # rerun_workflow_run: POST …/actions/runs/{id}/rerun — 201, like
            # the real Gitea; recorded for the call-count assertions
            run_id = int(path.split("/")[-2])
            self.server.reruns.append(run_id)
            self._json(201, {"message": "re-runned"})
            return
        # 409 "already exists" simulator: a pull request for this head is
        # already open on the remote (incident 6a438b3d) — create_pull must
        # surface PullRequestConflictError, not fail the story
        if self.server.pr_create_conflicts > 0:
            self.server.pr_create_conflicts -= 1
            self._json(409, {"message": "pull request already exists for this head"})
            return
        # transport blip simulator: fail the PR create with a 5xx N times
        # before letting it through (tests the bounded retry)
        if self.server.pr_create_failures > 0:
            self.server.pr_create_failures -= 1
            self._json(503, {"message": "gitea is warming up"})
            return
        created = {
            "number": 7,
            "merged": False,
            "mergeable": True,
            "state": "open",
        }
        if self.server.pr_html_url is not None:
            created["html_url"] = self.server.pr_html_url
        self._json(201, created)

    def do_PATCH(self):
        # close_pull: PATCH …/pulls/{index} {"state": "closed"}. A merged
        # PR cannot be closed — Gitea 409s (supersede must catch that).
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        body = json.loads(raw) if raw else None
        self._record(body)
        path = self.path.rstrip("/")
        index = int(path.split("/")[-1])
        info = self.server.pulls.get(index)
        if info is None:
            self._json(404, {"message": "pull request not found"})
            return
        if info.get("merged"):
            self._json(409, {"message": "pull request was already merged"})
            return
        if isinstance(body, dict) and body.get("state") == "closed":
            info["state"] = "closed"
        self._json(200, {"number": index, **info})

    def do_GET(self):
        self._record(None)
        path = self.path.split("?")[0].rstrip("/")
        if "/commits/" in path:
            sha = path.split("/commits/")[1].split("/")[0]
            # the per-context list endpoint (all_statuses): the full list,
            # because the combined view hides a marker only a superseded
            # context carries
            if path.endswith("/statuses"):
                found = self.server.statuses.get(sha)
                self._json(
                    200, list((found or {}).get("statuses") or [])
                )
                return
            found = self.server.statuses.get(sha)
            if found is None:
                self._json(200, {})
                return
            self._json(200, found)
            return
        if "/compare/" in path:
            # {base}...{head} — the reconcile sweep asks "is head contained?"
            # and the land-race recovery asks for the merge base
            ahead = self.server.compare_ahead
            if ahead is None:
                self._json(404, {"message": "compare not found"})
                return
            commits = [{"sha": f"c{i}"} for i in range(ahead)]
            payload = {"total_commits": ahead, "commits": commits}
            if self.server.merge_base is not None:
                payload["merge_base"] = self.server.merge_base
            self._json(200, payload)
            return
        if "/branches/" in path:
            head = self.server.branches.get(path.split("/branches/")[1])
            if head is None:
                self._json(404, {"message": "branch not found"})
                return
            self._json(200, {"name": "b", "commit": {"id": head}})
            return
        if "/actions/runs" in path:
            # find_latest_workflow_run: the repo's Actions runs, Gitea's
            # list shape. Served from `runs[head_sha] = [id, …]` so a test
            # can flip CI between calls without racing a thread.
            listing = []
            for sha, ids in self.server.runs.items():
                for run_id in ids:
                    listing.append({"id": run_id, "head_sha": sha})
            self._json(
                200,
                {"total_count": len(listing), "workflow_runs": listing},
            )
            return
        if path.endswith("/pulls"):
            # find_open_pull: the open PRs as a list, each with its head ref
            # (matched without the ?state=open&limit=… query)
            listing = [
                {
                    "number": idx,
                    "html_url": f"http://gitea.local/acme/app/pulls/{idx}",
                    "head": {"ref": f"fubala/{sid}"},
                }
                for sid, idx in self.server.open_pull_heads.items()
            ]
            self._json(200, listing)
            return
        if path.endswith("/files"):
            # the PR's changed files: the green-unmerged recovery asks
            # "is this a CI PR?" exactly like the land job does
            index = int(path[: -len("/files")].split("/")[-1])
            names = self.server.pull_files.get(index) or []
            self._json(200, [{"filename": n} for n in names])
            return
        index = int(path.split("/")[-1])
        info = self.server.pulls.get(index)
        if info is None:
            self._json(404, {"message": "pull request not found"})
            return
        # head.sha: the PR's current head commit — None unless the test
        # seeded pull_heads[index]. get_pull surfaces it so the poller can
        # judge CI on the CURRENT head, not a stale pushed_sha.
        self._json(
            200,
            {
                "number": index,
                **info,
                "head": {"ref": "b", "sha": self.server.pull_heads.get(index)},
                "base": {"ref": self.server.base_refs.get(index, "main")},
                "merge_base": self.server.pr_merge_base,
            },
        )
    def log_message(self, *args):
        pass  # keep pytest output clean


def _fake_gitea(
    pulls=None,
    statuses=None,
    pr_create_failures=0,
    compare_ahead=None,
    branches=None,
    pr_create_conflicts=0,
    open_pull_heads=None,
    pull_heads=None,
    merge_base=None,
    pr_merge_base=None,
    update_keeps_head=False,
    base_refs=None,
    update_refusals=0,
    update_up_to_date=0,
    update_server_errors=0,
    update_flip_merged=0,
    pull_files=None,
    runs=None,
    pr_html_url="http://gitea.local/acme/app/pulls/7",
):
    """Fake Gitea with PR create/read, update, compare and branch endpoints.

    pr_html_url = str|None   # html_url of a created PR; None leaves the
                             # field out of the response
    Knobs a test can flip while the server runs:
      pulls[index] = {"merged": …, "mergeable": …, "state": …,
                      "merge_commit_sha": …}
      statuses[sha] = {"status": …, "statuses": [...]}
      pr_create_failures = N   # first N POSTs of the PR create 503
      pr_create_conflicts = N  # first N POSTs of the PR create 409
                               # ("already exists" — the incident shape)
      compare_ahead = int|None # compare {base}...{head}: commits ahead of
                                # base; None => 404 (deleted/unknown)
      branches[name] = sha     # GET /branches/{name}; missing => 404
      open_pull_heads[story_id] = index
                               # GET /pulls?state=open: an open PR whose head
                               # is fubala/<story_id> with the given number
      pull_heads[index] = sha  # GET /pulls/{index}: the PR's head commit,
                               # served as head: {ref, sha}. A POST …/update
                               # moves it (<sha>-updated), like a real branch
                               # update would. An unseeded index serves
                               # head.sha None — the poller then falls back
                               # to pushed_sha (legacy fakes keep passing).
      merge_base = sha|None    # compare {base}...{head}: the merge_base
                               # field; None => absent from the payload (the
                               # land-race check then reads "not behind")
      pr_merge_base = sha|None # GET /pulls/{index}: the PR payload's
                               # merge_base (what real Gitea serves); None
                               # => null, the compare field is the fallback
      update_keeps_head = bool # a POST …/update leaves the PR head as is
      base_refs[index] = ref   # GET /pulls/{index}: base.ref; an unseeded
                               # index serves "main"
      update_refusals = N      # first N POSTs of …/update 409 "cannot
                               # update branch" (a conflict the base
                               # introduced); 0 lets them through
      update_up_to_date = N    # first N POSTs of …/update answer 500
                               # {"message": "HeadBranch of PR <index> is up
                               # to date"} — Gitea's misused 500 for "the
                               # branch already contains its base"; 0 lets
                               # them through
      update_server_errors = N # first N POSTs of …/update answer a plain
                               # 500 {"message": "internal server error"}
                               # (overload noise; NOT the up-to-date text)
      runs[sha] = [id, …]      # GET /actions/runs: Actions runs as
                               # {"id", "head_sha": sha}; POST …/runs/{id}/
                               # rerun records into server.reruns (201)
      update_flip_merged = N   # first N POSTs of …/update answer 422 AND
                               # flip the PR to merged right then — the PR
                               # merged between the poller's read and the
                               # POST (deterministic; no thread race)
      pull_files[index] = ["path", …]
                               # GET /pulls/{index}/files: the PR's changed
                               # file names; an unseeded index serves []
                               # (not proven to be a CI PR)
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PRHandler)
    server.requests = []
    server.pulls = pulls or {}
    server.statuses = statuses or {}
    server.pr_create_failures = pr_create_failures
    server.pr_create_conflicts = pr_create_conflicts
    server.compare_ahead = compare_ahead
    server.branches = branches or {}
    server.open_pull_heads = dict(open_pull_heads or {})
    server.pull_heads = dict(pull_heads or {})
    server.merge_base = merge_base
    server.pr_merge_base = pr_merge_base
    server.update_keeps_head = update_keeps_head
    server.base_refs = dict(base_refs or {})
    server.update_refusals = update_refusals
    server.update_flip_merged = update_flip_merged
    server.update_server_errors = update_server_errors
    server.pull_files = dict(pull_files or {})
    server.update_up_to_date = update_up_to_date
    server.runs = dict(runs or {})
    server.reruns = []
    server.pr_html_url = pr_html_url
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_approve_in_push_mode_opens_pr_and_never_leaks_the_token(
    tmp_path, monkeypatch
):
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    secret = "super-secret-token-do-not-log"
    token_file.write_text(secret + "\n")

    server = _fake_gitea()
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "pr.db"))
    try:
        with TestClient(create_app(gateway=ScriptedGW())) as client:
            client.patch(
                "/api/v1/settings",
                json={
                    "story_poll_interval": 0.05,
                    "project_root": repo,
                    "write_tools_enabled": True,
                },
            )
            project = client.post(
                "/api/v1/projects",
                json={
                    "name": "p",
                    "root": repo,
                    "delivery": "push",
                    "pr_api_base": f"http://127.0.0.1:{server.server_address[1]}",
                    "pr_repo": "acme/app",
                    "pr_base_branch": "main",
                    "pr_token_file": str(token_file),
                },
            ).json()
            story = client.post(
                "/api/v1/stories",
                json={"title": "feature", "project_id": project["id"]},
            ).json()
            sid = story["id"]
            current = run_to_human_review(client, sid)
            assert current["status"] == "human_review", current.get("error")

            final = client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"}).json()
            # a PR is open: landing, not done — done only follows a merge
            assert final["status"] == "landing", final
            assert final["pr_url"] == "http://gitea.local/acme/app/pulls/7"
            assert final["pr_index"] == 7

            # exactly one PR call, to the Gitea pulls endpoint, token-authed
            posts = [r for r in server.requests if r["method"] == "POST"]
            assert len(posts) == 1, server.requests
            req = posts[0]
            assert req["path"] == "/api/v1/repos/acme/app/pulls"
            assert req["auth"] == f"token {secret}"
            assert req["body"]["head"] == f"fubala/{sid}"
            assert req["body"]["base"] == "main"
            # title is the story title (falling back to the story id)
            assert req["body"]["title"] == "feature"
            assert sid in req["body"]["body"]

            detail = client.get(f"/api/v1/stories/{sid}").json()
            notes = [n for n in note_events(detail) if n.get("delivery") == "push"]
            assert notes[-1].get("pr_url") == "http://gitea.local/acme/app/pulls/7"
            # token hygiene: the secret never reaches the API surface
            assert secret not in json.dumps(detail)
            assert secret not in json.dumps(final)
    finally:
        server.shutdown()
        server.server_close()


def test_approve_in_push_mode_missing_token_file_fails_the_story(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    missing = tmp_path / "nope" / "token.txt"
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "fail.db"))

    with TestClient(create_app(gateway=ScriptedGW())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
            },
        )
        project = client.post(
            "/api/v1/projects",
            json={
                "name": "p",
                "root": repo,
                "delivery": "push",
                "pr_api_base": "http://127.0.0.1:1",
                "pr_repo": "acme/app",
                "pr_base_branch": "main",
                "pr_token_file": str(missing),
            },
        ).json()
        story = client.post(
            "/api/v1/stories",
            json={"title": "feature", "project_id": project["id"]},
        ).json()
        sid = story["id"]
        current = run_to_human_review(client, sid)
        assert current["status"] == "human_review", current.get("error")

        res = client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
        assert res.status_code == 409
        detail = res.json()["detail"]
        assert detail.startswith("push failed:")
        # path-level error only: the missing path is named, nothing else leaks
        assert str(missing) in detail

        after = client.get(f"/api/v1/stories/{sid}").json()
        assert after["status"] == "failed"
        assert str(missing) in (after["error"] or "")
        # the worktree survives: it is the only copy of the work
        assert (Path(repo) / ".fubala-worktrees" / sid).exists()
        notes = [n for n in note_events(after) if n.get("delivery") == "push"]
        assert notes and notes[-1].get("error")
        # the branch DID push before the PR stage failed — evidence survives
        assert notes[-1].get("pushed_sha")


def test_approve_in_push_mode_broken_remote_fails_the_story(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    # remote points nowhere: the push itself fails
    subprocess.run(
        ["git", "-C", repo, "remote", "add", "origin", str(tmp_path / "missing.git")],
        check=True,
    )
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "gitfail.db"))

    with TestClient(create_app(gateway=ScriptedGW())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
            },
        )
        project = client.post(
            "/api/v1/projects", json={"name": "p", "root": repo, "delivery": "push"}
        ).json()
        story = client.post(
            "/api/v1/stories",
            json={"title": "feature", "project_id": project["id"]},
        ).json()
        sid = story["id"]
        current = run_to_human_review(client, sid)
        assert current["status"] == "human_review", current.get("error")

        res = client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
        assert res.status_code == 409
        assert "push failed:" in res.json()["detail"]
        after = client.get(f"/api/v1/stories/{sid}").json()
        assert after["status"] == "failed"
        assert after["error"]
        assert (Path(repo) / ".fubala-worktrees" / sid).exists()


# ---- landing poller --------------------------------------------------------


def _push_project(client, repo: str, server, token_file) -> str:
    project = client.post(
        "/api/v1/projects",
        json={
            "name": "p",
            "root": repo,
            "delivery": "push",
            "pr_api_base": f"http://127.0.0.1:{server.server_address[1]}",
            "pr_repo": "acme/app",
            "pr_base_branch": "main",
            "pr_token_file": str(token_file),
        },
    ).json()
    return project["id"]


@pytest.fixture(autouse=True)
def _fast_land_poll(monkeypatch):
    """The poller's Gitea spacing would starve these tests: sweep freely."""
    from app import runner as runner_module

    monkeypatch.setattr(runner_module, "LAND_POLL_MIN_INTERVAL", 0.0)


def test_landed_pr_reaches_done_via_the_poller(tmp_path, monkeypatch):
    """Approve in push+PR mode => landing; merged => done (error cleared)."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(pulls={7: {"merged": True, "mergeable": True, "state": "open"}})
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "land.db"))
    try:
        with TestClient(create_app(gateway=ScriptedGW())) as client:
            client.patch(
                "/api/v1/settings",
                json={
                    "story_poll_interval": 0.05,
                    "project_root": repo,
                    "write_tools_enabled": True,
                },
            )
            pid = _push_project(client, repo, server, token_file)
            story = client.post(
                "/api/v1/stories", json={"title": "feature", "project_id": pid}
            ).json()
            sid = story["id"]
            run_to_human_review(client, sid)

            approved = client.post(
                f"/api/v1/stories/{sid}/approve", json={"by": "t"}
            ).json()
            assert approved["status"] == "landing", approved
            assert approved["pr_index"] == 7
            assert approved["pr_url"] == "http://gitea.local/acme/app/pulls/7"

            done = wait_status(client, sid, ("done",))
            assert done["error"] is None
            assert done["pr_index"] == 7
    finally:
        server.shutdown()
        server.server_close()


def test_reapprove_after_land_failure_and_rerun_reuses_the_open_pr(
    tmp_path, monkeypatch
):
    """Incident 6a438b3d, end to end: land fails (merge conflict) -> reject ->
    coder rerun -> approve again. The PR from round one is still open, so the
    second approve must reuse it: exactly one POST /pulls in total, pr_index/
    pr_url unchanged, story back in `landing` (then done on merge) — never a
    409-spawned `failed`."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": False, "state": "open"}}
    )
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "incident.db"))
    try:
        with TestClient(create_app(gateway=ScriptedGW())) as client:
            client.patch(
                "/api/v1/settings",
                json={
                    "story_poll_interval": 0.05,
                    "project_root": repo,
                    "write_tools_enabled": True,
                },
            )
            pid = _push_project(client, repo, server, token_file)
            story = client.post(
                "/api/v1/stories", json={"title": "feature", "project_id": pid}
            ).json()
            sid = story["id"]
            run_to_human_review(client, sid)

            # round one: approve -> landing -> the poller bounces it back to
            # the gate with the conflict
            first = client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
            assert first.status_code == 200, first.text
            assert first.json()["status"] == "landing"
            gate = wait_status(client, sid, ("human_review",), timeout=30.0)
            assert gate["error"] == "land failed: merge conflict"
            assert gate["pr_index"] == 7
            original_pr_url = gate["pr_url"]

            # the human rejects with feedback; the coder rerun recreates the
            # worktree (fresh off main) and writes the same feature again —
            # this is the path that used to bypass the re-land shortcut
            rejected = client.post(
                f"/api/v1/stories/{sid}/reject",
                json={"by": "t", "reason": "fix the conflict"},
            )
            assert rejected.status_code == 200, rejected.text
            rerun = wait_status(client, sid, ("human_review",), timeout=60.0)
            assert rerun["status"] == "human_review", rerun.get("error")
            assert (Path(repo) / ".fubala-worktrees" / sid).exists()

            # round two: the PR is still open (state open, not merged) —
            # approve must re-enter landing on THAT pull request
            second = client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
            assert second.status_code == 200, second.text
            body = second.json()
            assert body["status"] == "landing", body
            assert body["error"] is None
            assert body["pr_index"] == 7
            assert body["pr_url"] == original_pr_url

            # exactly one PR create across both approvals
            creates = [
                r
                for r in server.requests
                if r["method"] == "POST" and r["path"].endswith("/pulls")
            ]
            assert len(creates) == 1, server.requests

            # and the reused watch still carries the story to done on merge
            server.pulls[7] = {"merged": True, "mergeable": True, "state": "open"}
            done = wait_status(client, sid, ("done",))
            assert done["error"] is None
            assert done["pr_index"] == 7
            assert done["pr_url"] == original_pr_url

            # the reuse is visible in the event trail
            detail = client.get(f"/api/v1/stories/{sid}").json()
            notes = [n for n in note_events(detail) if n.get("delivery") == "push"]
            reuse_notes = [n for n in notes if n.get("reused_pr")]
            assert reuse_notes, notes
            assert reuse_notes[-1].get("pr_index") == 7
    finally:
        server.shutdown()
        server.server_close()


def test_create_pull_409_adopts_the_existing_open_pr(tmp_path, monkeypatch):
    """Second shape of the incident: the recorded PR (7) was closed by a
    human, so the re-approval falls through to create_pull — which 409s
    "already exists" because PR 8 (same head) is open. The conflict must
    resolve to PR 8 and land the story, never fail it."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": False, "state": "open"}}
    )
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "conflict.db"))
    try:
        with TestClient(create_app(gateway=ScriptedGW())) as client:
            client.patch(
                "/api/v1/settings",
                json={
                    "story_poll_interval": 0.05,
                    "project_root": repo,
                    "write_tools_enabled": True,
                },
            )
            pid = _push_project(client, repo, server, token_file)
            story = client.post(
                "/api/v1/stories", json={"title": "feature", "project_id": pid}
            ).json()
            sid = story["id"]
            run_to_human_review(client, sid)

            # round one: PR 7 opens, the conflict bounces the story back
            first = client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
            assert first.status_code == 200, first.text
            gate = wait_status(client, sid, ("human_review",), timeout=30.0)
            assert gate["pr_index"] == 7

            # reject -> coder rerun recreates the worktree, so the next
            # approve takes the full push+PR path (not the re-land shortcut)
            rejected = client.post(
                f"/api/v1/stories/{sid}/reject",
                json={"by": "t", "reason": "fix the conflict"},
            )
            assert rejected.status_code == 200, rejected.text
            rerun = wait_status(client, sid, ("human_review",), timeout=60.0)
            assert rerun["status"] == "human_review", rerun.get("error")
            assert (Path(repo) / ".fubala-worktrees" / sid).exists()

            # meanwhile PR 7 was closed by hand and PR 8 exists for the same
            # head; Gitea refuses a third create with 409 "already exists"
            server.pulls[7] = {"merged": False, "mergeable": True, "state": "closed"}
            server.pulls[8] = {"merged": False, "mergeable": True, "state": "open"}
            server.open_pull_heads[sid] = 8
            server.pr_create_conflicts = 1

            second = client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
            assert second.status_code == 200, second.text
            body = second.json()
            assert body["status"] == "landing", body
            assert body["error"] is None
            assert body["pr_index"] == 8
            assert body["pr_url"] == "http://gitea.local/acme/app/pulls/8"

            # exactly two create POSTs across both approvals: round one's
            # successful create of PR 7 and the 409'd create — a third would
            # mean the conflict was retried blindly instead of adopted
            creates = [
                r
                for r in server.requests
                if r["method"] == "POST" and r["path"].endswith("/pulls")
            ]
            assert len(creates) == 2, server.requests

            # the adopted watch carries the story to done on PR 8's merge
            server.pulls[8] = {"merged": True, "mergeable": True, "state": "open"}
            done = wait_status(client, sid, ("done",))
            assert done["error"] is None
            assert done["pr_index"] == 8

            detail = client.get(f"/api/v1/stories/{sid}").json()
            notes = [n for n in note_events(detail) if n.get("delivery") == "push"]
            resolved = [n for n in notes if n.get("resolved_409")]
            assert resolved, notes
            assert resolved[-1].get("pr_index") == 8
    finally:
        server.shutdown()
        server.server_close()


def test_failed_ci_status_returns_the_story_to_the_gate(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(pulls={7: {"merged": False, "mergeable": True, "state": "open"}})
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "cifail.db"))
    try:
        with TestClient(create_app(gateway=ScriptedGW())) as client:
            client.patch(
                "/api/v1/settings",
                json={
                    "story_poll_interval": 0.05,
                    "project_root": repo,
                    "write_tools_enabled": True,
                },
            )
            pid = _push_project(client, repo, server, token_file)
            story = client.post(
                "/api/v1/stories", json={"title": "feature", "project_id": pid}
            ).json()
            sid = story["id"]
            run_to_human_review(client, sid)
            approved = client.post(
                f"/api/v1/stories/{sid}/approve", json={"by": "t"}
            ).json()
            assert approved["status"] == "landing"

            # the pushed sha fails its combined commit status
            server.statuses[approved["pushed_sha"]] = {
                "status": "failure",
                "statuses": [
                    {"status": "success", "description": "lint ok"},
                    {"status": "failure", "description": "build broke on py3.12"},
                ],
            }
            gate = wait_status(client, sid, ("human_review",))
            assert gate["error"].startswith("land failed:")
            assert "build broke on py3.12" in gate["error"]
            # visible in the board listing too
            listed = client.get("/api/v1/stories").json()
            row = next(s for s in listed if s["id"] == sid)
            assert row["status"] == "human_review"
            assert row["error"].startswith("land failed:")
    finally:
        server.shutdown()
        server.server_close()


def test_failure_on_old_head_sha_after_branch_update_keeps_the_story_landing(
    tmp_path, monkeypatch
):
    """Incident f9014e83: retry-land updates the branch, so the PR's head
    moves and the failure recorded on the PREVIOUS head must not bounce a
    landing story — only CI on the CURRENT head counts."""
    from app import runner as runner_module

    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": False, "state": "open"}},
        pull_heads={7: "aaa"},
    )
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "stalehead.db"))
    monkeypatch.setattr(runner_module, "LAND_POLL_MIN_INTERVAL", 0.0)
    try:
        with TestClient(create_app(gateway=ScriptedGW())) as client:
            client.patch(
                "/api/v1/settings",
                json={
                    "story_poll_interval": 0.05,
                    "project_root": repo,
                    "write_tools_enabled": True,
                },
            )
            pid = _push_project(client, repo, server, token_file)
            story = client.post(
                "/api/v1/stories", json={"title": "feature", "project_id": pid}
            ).json()
            sid = story["id"]
            run_to_human_review(client, sid)
            client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
            gate = wait_status(client, sid, ("human_review",), timeout=30.0)
            assert gate["error"] == "land failed: merge conflict"

            # the operator fixed the base and retries: the update moves the
            # PR head from aaa to aaa-updated (the fake mirrors Gitea)
            server.pulls[7] = {"merged": False, "mergeable": True, "state": "open"}
            retried = client.post(
                f"/api/v1/stories/{sid}/retry-land", json={"by": "t"}
            ).json()
            assert retried["status"] == "landing", retried
            assert server.pull_heads[7] == "aaa-updated"

            # CI red on the OLD head (the stale verdict of the incident),
            # green on the CURRENT one: the story must stay landing
            server.statuses["aaa"] = {
                "status": "failure",
                "statuses": [{"status": "failure", "description": "build broke"}],
            }
            server.statuses["aaa-updated"] = {
                "status": "success",
                "statuses": [{"status": "success", "description": "lint ok"}],
            }
            time.sleep(0.4)
            still = client.get(f"/api/v1/stories/{sid}").json()
            assert still["status"] == "landing", still
            assert still["error"] is None, still

            # and the watch still carries it to done on merge
            server.pulls[7] = {"merged": True, "mergeable": True, "state": "open"}
            done = wait_status(client, sid, ("done",))
            assert done["error"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_poller_closes_gate_story_whose_pr_merged(tmp_path, monkeypatch):
    """Incident 6a438b3d: a story parked at the human gate with a
    `land failed:` error closes as done once its PR merges anyway. A
    non-merged gate story is left untouched."""
    from app import runner as runner_module

    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(pulls={7: {"merged": False, "mergeable": True, "state": "open"}})
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "gatemerge.db"))
    monkeypatch.setattr(runner_module, "LAND_POLL_MIN_INTERVAL", 0.0)
    try:
        with TestClient(create_app(gateway=ScriptedGW())) as client:
            client.patch(
                "/api/v1/settings",
                json={
                    "story_poll_interval": 0.05,
                    "project_root": repo,
                    "write_tools_enabled": True,
                },
            )
            pid = _push_project(client, repo, server, token_file)
            story = client.post(
                "/api/v1/stories", json={"title": "feature", "project_id": pid}
            ).json()
            sid = story["id"]
            run_to_human_review(client, sid)
            approved = client.post(
                f"/api/v1/stories/{sid}/approve", json={"by": "t"}
            ).json()
            assert approved["status"] == "landing"

            # CI fails on the pushed sha: the poller bounces the story to
            # the gate (head.sha is unseeded in the fake, so the poller
            # falls back to pushed_sha — the legacy shape)
            server.statuses[approved["pushed_sha"]] = {
                "status": "failure",
                "statuses": [{"status": "failure", "description": "build broke"}],
            }
            gate = wait_status(client, sid, ("human_review",), timeout=30.0)
            assert gate["error"] == "land failed: build broke"

            # the PR is still open: the sweep must NOT touch the gate row
            time.sleep(0.3)
            untouched = client.get(f"/api/v1/stories/{sid}").json()
            assert untouched["status"] == "human_review", untouched
            assert untouched["error"] == "land failed: build broke"

            # someone merges the PR in Gitea anyway: the next sweep sees
            # merged and closes the story done, error cleared
            server.pulls[7] = {"merged": True, "mergeable": True, "state": "open"}
            done = wait_status(client, sid, ("done",), timeout=30.0)
            assert done["error"] is None
            assert done["pr_index"] == 7
    finally:
        server.shutdown()
        server.server_close()


def test_merge_conflict_needs_two_consecutive_false_polls(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": False, "state": "open"}}
    )
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "conflict.db"))
    try:
        with TestClient(create_app(gateway=ScriptedGW())) as client:
            client.patch(
                "/api/v1/settings",
                json={
                    "story_poll_interval": 0.05,
                    "project_root": repo,
                    "write_tools_enabled": True,
                },
            )
            pid = _push_project(client, repo, server, token_file)
            story = client.post(
                "/api/v1/stories", json={"title": "feature", "project_id": pid}
            ).json()
            sid = story["id"]
            run_to_human_review(client, sid)
            approved = client.post(
                f"/api/v1/stories/{sid}/approve", json={"by": "t"}
            ).json()
            assert approved["status"] == "landing"

            # a single False observation is not a verdict…
            time.sleep(0.6)
            still = client.get(f"/api/v1/stories/{sid}").json()
            assert still["status"] == "landing", still

            # …the second consecutive one is
            gate = wait_status(client, sid, ("human_review",), timeout=30.0)
            assert gate["error"] == "land failed: merge conflict"
    finally:
        server.shutdown()
        server.server_close()


def test_retry_land_posts_the_update_endpoint_and_relands(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": False, "state": "open"}}
    )
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "retry.db"))
    try:
        with TestClient(create_app(gateway=ScriptedGW())) as client:
            client.patch(
                "/api/v1/settings",
                json={
                    "story_poll_interval": 0.05,
                    "project_root": repo,
                    "write_tools_enabled": True,
                },
            )
            pid = _push_project(client, repo, server, token_file)
            story = client.post(
                "/api/v1/stories", json={"title": "feature", "project_id": pid}
            ).json()
            sid = story["id"]
            run_to_human_review(client, sid)
            client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
            gate = wait_status(client, sid, ("human_review",), timeout=30.0)
            assert gate["error"] == "land failed: merge conflict"

            # the operator fixed the base: retry re-lands without re-pushing
            server.pulls[7] = {"merged": False, "mergeable": True, "state": "open"}
            retried = client.post(
                f"/api/v1/stories/{sid}/retry-land", json={"by": "t"}
            ).json()
            assert retried["status"] == "landing", retried
            assert retried["error"] is None
            updates = [
                r
                for r in server.requests
                if r["method"] == "POST" and r["path"].split("?")[0].endswith("/update")
            ]
            assert updates, server.requests
            assert (
                updates[-1]["path"]
                == "/api/v1/repos/acme/app/pulls/7/update?style=merge"
            )
            assert updates[-1]["auth"] == "token sekrit"

            # and the poller carries it to done once the PR merges
            server.pulls[7] = {"merged": True, "mergeable": True, "state": "open"}
            done = wait_status(client, sid, ("done",))
            assert done["error"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_retry_land_on_merged_pr_returns_done_not_409(tmp_path, monkeypatch):
    """Incident 6a438b3d, endpoint side: retry-land against a PR that merged
    meanwhile must close the story done with a 200 — Gitea's 422 "already
    merged" must never surface as `land failed:` + 409. The verdict comes
    from the PR state, never from message strings."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": False, "state": "open"}}
    )
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "mergedretry.db"))
    try:
        with TestClient(create_app(gateway=ScriptedGW())) as client:
            client.patch(
                "/api/v1/settings",
                json={
                    "story_poll_interval": 0.05,
                    "project_root": repo,
                    "write_tools_enabled": True,
                },
            )
            pid = _push_project(client, repo, server, token_file)
            story = client.post(
                "/api/v1/stories", json={"title": "feature", "project_id": pid}
            ).json()
            sid = story["id"]
            run_to_human_review(client, sid)
            client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
            gate = wait_status(client, sid, ("human_review",), timeout=30.0)
            assert gate["error"] == "land failed: merge conflict"

            # HARD-off the scheduler across the retry: the landing poller ALSO
            # watches gate stories whose PR merged in the background, so a
            # tick firing between the pull flip below and the POST can close
            # the story done first — the POST then 409s ("story is done") and
            # the race, not the endpoint, fails the test (it hit on a loaded
            # box, serial or xdist alike). runner.stop() awaits the cancelled
            # loop task, so no tick can interleave; retry-land needs no loop.
            runner = client.app.state.story_runner
            client.portal.call(runner.stop)

            # the PR merges before the operator (or an MCP agent) retries
            server.pulls[7] = {"merged": True, "mergeable": True, "state": "open"}
            response = client.post(f"/api/v1/stories/{sid}/retry-land", json={"by": "t"})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["status"] == "done", body
            assert body["error"] is None
            assert body["pr_index"] == 7

            # no branch-update POST was even attempted, and no 409/land
            # failure note exists in the trail
            updates = [
                r
                for r in server.requests
                if r["method"] == "POST" and r["path"].split("?")[0].endswith("/update")
            ]
            assert updates == [], server.requests
            detail = client.get(f"/api/v1/stories/{sid}").json()
            notes = [n for n in note_events(detail) if n.get("delivery") == "push"]
            assert all("error" not in n for n in notes), notes
            merged_notes = [n for n in notes if n.get("merged")]
            assert merged_notes, notes
    finally:
        server.shutdown()
        server.server_close()


def test_retry_land_on_up_to_date_500_reruns_ci_and_lands(tmp_path, monkeypatch):
    """Incident: the branch is ALREADY up to date — Gitea answers the update
    POST with a misused 500 "HeadBranch of PR 7 is up to date". The retry-land
    recovery must not burn three attempts into "transient network error": it
    takes the up-to-date branch, re-runs CI on the unchanged head and the
    poller lands the story when the run goes green."""
    from app import runner as runner_module

    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": False, "state": "open"}},
        update_up_to_date=1,
        pull_heads={7: "aaa"},
        runs={"aaa": [41]},
    )
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "uptodate.db"))
    monkeypatch.setattr(runner_module, "LAND_POLL_MIN_INTERVAL", 0.0)
    try:
        with TestClient(create_app(gateway=ScriptedGW())) as client:
            client.patch(
                "/api/v1/settings",
                json={
                    "story_poll_interval": 0.05,
                    "project_root": repo,
                    "write_tools_enabled": True,
                },
            )
            pid = _push_project(client, repo, server, token_file)
            story = client.post(
                "/api/v1/stories", json={"title": "feature", "project_id": pid}
            ).json()
            sid = story["id"]
            run_to_human_review(client, sid)
            client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
            gate = wait_status(client, sid, ("human_review",), timeout=30.0)
            assert gate["error"] == "land failed: merge conflict"

            # the operator fixed the base, but the branch already contains
            # it: the update POST 500s "up to date" — the recovery must
            # rerun CI on the unchanged head, not fail the story
            server.pulls[7] = {"merged": False, "mergeable": True, "state": "open"}
            retried = client.post(
                f"/api/v1/stories/{sid}/retry-land", json={"by": "t"}
            )
            assert retried.status_code == 200, retried.text
            body = retried.json()
            assert body["status"] == "landing", body
            assert body["error"] is None

            # exactly ONE update POST (no transient retry loop) and exactly
            # ONE rerun POST of the head's latest run
            updates = [
                r
                for r in server.requests
                if r["method"] == "POST"
                and r["path"].split("?")[0].endswith("/update")
            ]
            assert len(updates) == 1, server.requests
            assert updates[0]["path"] == "/api/v1/repos/acme/app/pulls/7/update?style=merge"
            assert server.reruns == [41], server.reruns
            reruns = [
                r
                for r in server.requests
                if r["method"] == "POST" and r["path"].endswith("/rerun")
            ]
            assert reruns == [
                {
                    "method": "POST",
                    "path": "/api/v1/repos/acme/app/actions/runs/41/rerun",
                    "auth": "token sekrit",
                    "accept": "application/json",
                    "body": None,
                }
            ], server.requests

            # "transient network error" appears nowhere in the trail
            detail = client.get(f"/api/v1/stories/{sid}").json()
            assert "transient network error" not in json.dumps(detail)
            notes = [n for n in note_events(detail) if n.get("delivery") == "push"]
            rerun_notes = [n for n in notes if n.get("up_to_date")]
            assert rerun_notes, notes
            assert rerun_notes[-1].get("run_id") == 41

            # CI goes green on the (unchanged) head and the PR merges: done
            server.statuses["aaa"] = {
                "status": "success",
                "statuses": [{"status": "success", "description": "lint ok"}],
            }
            server.pulls[7] = {"merged": True, "mergeable": True, "state": "open"}
            done = wait_status(client, sid, ("done",))
            assert done["error"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_retry_land_other_500_on_update_still_transient_fails(tmp_path, monkeypatch):
    """A plain 500 on the update POST (overload noise, not the up-to-date
    text) must keep today's shape: three attempts, then `land failed:
    transient network error (3 attempts)` and a 409."""
    from app import pr as pr_module

    monkeypatch.setattr(pr_module, "PR_BACKOFF_BASE", 0.0)
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": False, "state": "open"}},
        update_server_errors=99,
        pull_heads={7: "aaa"},
    )
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "other500.db"))
    try:
        with TestClient(create_app(gateway=ScriptedGW())) as client:
            client.patch(
                "/api/v1/settings",
                json={
                    "story_poll_interval": 0.05,
                    "project_root": repo,
                    "write_tools_enabled": True,
                },
            )
            pid = _push_project(client, repo, server, token_file)
            story = client.post(
                "/api/v1/stories", json={"title": "feature", "project_id": pid}
            ).json()
            sid = story["id"]
            run_to_human_review(client, sid)
            client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
            gate = wait_status(client, sid, ("human_review",), timeout=30.0)
            assert gate["error"] == "land failed: merge conflict"

            # HARD-off the scheduler: with the 500s answering, a concurrent
            # poll tick racing the POST is harmless (merged stays False), but
            # the hard-off keeps the failure deterministic — same guard the
            # merged-PR test uses.
            runner = client.app.state.story_runner
            client.portal.call(runner.stop)

            response = client.post(
                f"/api/v1/stories/{sid}/retry-land", json={"by": "t"}
            )
            assert response.status_code == 409, response.text
            detail = client.get(f"/api/v1/stories/{sid}").json()
            assert detail["status"] == "human_review", detail
            # the message keeps today's shape: retried 3x, then the last
            # error's text as the suffix
            assert detail["error"] and detail["error"].startswith(
                "land failed: transient network error (3 attempts)"
            ), detail
            updates = [
                r
                for r in server.requests
                if r["method"] == "POST"
                and r["path"].split("?")[0].endswith("/update")
            ]
            assert len(updates) == 3, server.requests
            # no rerun was attempted on the failure path
            assert server.reruns == [], server.reruns
    finally:
        server.shutdown()
        server.server_close()


def test_landing_story_survives_its_project_losing_pr_config(tmp_path, monkeypatch):
    """Regression: a story mid-landing whose project drops its pr_* config
    must be parked at the human gate with the reason — not spin in landing
    forever. The transition itself used to crash inside _watch_one_landing
    (bad arity into _landing_publish), which the poller swallowed as a
    per-story exception, leaving the story landing permanently."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}}
    )
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "noconf.db"))
    try:
        with TestClient(create_app(gateway=ScriptedGW())) as client:
            client.patch(
                "/api/v1/settings",
                json={
                    "story_poll_interval": 0.05,
                    "project_root": repo,
                    "write_tools_enabled": True,
                },
            )
            pid = _push_project(client, repo, server, token_file)
            story = client.post(
                "/api/v1/stories", json={"title": "feature", "project_id": pid}
            ).json()
            sid = story["id"]
            run_to_human_review(client, sid)
            approved = client.post(
                f"/api/v1/stories/{sid}/approve", json={"by": "t"}
            ).json()
            assert approved["status"] == "landing"

            # the project is reconfigured mid-flight: no Gitea to poll anymore
            client.patch(
                f"/api/v1/projects/{pid}", json={"pr_api_base": None}
            )
            gate = wait_status(client, sid, ("human_review",), timeout=30.0)
            assert gate["error"] == "land failed: pull request configuration missing"
            assert gate["pr_index"] == 7  # the PR facts survive, retry-land can use them

            # and once the config is restored, the gate story re-lands on approve
            client.patch(
                f"/api/v1/projects/{pid}",
                json={"pr_api_base": f"http://127.0.0.1:{server.server_address[1]}"},
            )
            re_land = client.post(
                f"/api/v1/stories/{sid}/approve", json={"by": "t"}
            ).json()
            assert re_land["status"] == "landing", re_land
            server.pulls[7] = {"merged": True, "mergeable": True, "state": "open"}
            done = wait_status(client, sid, ("done",))
            assert done["error"] is None
    finally:
        server.shutdown()
        server.server_close()


# ---- pr module ------------------------------------------------------------


def test_pr_error_messages_are_body_truncated_and_token_free():
    from app.pr import PullRequestError, create_pull, read_token

    server = ThreadingHTTPServer(("127.0.0.1", 0), _BigBodyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(PullRequestError) as exc:
            create_pull(
                base_url=f"http://127.0.0.1:{server.server_address[1]}",
                repo="acme/app",
                token="sekrit",
                head="fubala/x",
                base_branch="main",
                title="t",
            )
        message = str(exc.value)
        assert "422" in message
        assert "sekrit" not in message
        assert len(message) < 900  # the huge body is capped
    finally:
        server.shutdown()
        server.server_close()

    with pytest.raises(PullRequestError) as exc:
        read_token("/definitely/not/here/token.txt")
    assert "/definitely/not/here/token.txt" in str(exc.value)
    # an empty token file is just as unusable
    empty = Path(tempfile.mkdtemp()) / "empty.txt"
    empty.write_text("   \n")
    with pytest.raises(PullRequestError) as exc:
        read_token(str(empty))
    assert "empty" in str(exc.value)


# ---- approve: merge mode must be unchanged ---------------------------------


def test_approve_in_merge_mode_for_a_project_story_still_merges(tmp_path, monkeypatch):
    """Regression (AC: merge behavior byte-for-byte unchanged): a story bound
    to a project that stays on the default 'merge' delivery keeps the original
    approve path — merged into the ROOT checkout, 'merged': True in the status
    event, worktree discarded, no push-mode note events."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "merge.db"))

    with TestClient(create_app(gateway=ScriptedGW())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
            },
        )
        project = client.post(
            "/api/v1/projects", json={"name": "p", "root": repo}
        ).json()
        assert project["delivery"] == "merge"
        story = client.post(
            "/api/v1/stories",
            json={"title": "feature", "project_id": project["id"]},
        ).json()
        sid = story["id"]
        run_to_human_review(client, sid)

        final = client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"}).json()
        assert final["status"] == "done"

        # the work landed in the root checkout and the worktree is gone
        assert (Path(repo) / "feature.py").read_text() == "VALUE = 'pushed'\n"
        assert not (Path(repo) / ".fubala-worktrees" / sid).exists()

        detail = client.get(f"/api/v1/stories/{sid}").json()
        status_events = [
            json.loads(e.get("data") or "{}")
            for e in detail.get("events", [])
            if e.get("kind") == "status"
        ]
        assert status_events[-1].get("merged") is True
        assert "pushed" not in status_events[-1]
        assert not [n for n in note_events(detail) if n.get("delivery") == "push"]


class _BigBodyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        payload = (b"x" * 50_000).decode("ascii", "replace")
        body = json.dumps({"message": payload}).encode()
        self.send_response(422)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


# ---- land race: the suite was green but main moved before the merge --------


def _race_statuses(extra_success=True):
    """The exact three-entry shape of a lost land race on one sha.

    The land context failed (the merge was refused), while the SAME sha
    carries an earlier green test context whose description holds the
    marker. Combined status / latest-per-context hides the marker, so the
    recovery reads the full list.
    """
    entries = [
        {
            "status": "failure",
            "context": "ci/acme app/ land (pull_request)",
            "description": "merge refused: base branch moved",
        },
        {
            "status": "success",
            "context": "ci/acme app/ test (pull_request)",
            "description": "suite green on the merge result",
        },
    ]
    if extra_success:
        entries.append(
            {"status": "success", "context": "ci/lint", "description": "lint ok"}
        )
    return {"status": "failure", "statuses": entries}


def _update_calls(server):
    return [
        r
        for r in server.requests
        if r["method"] == "POST" and r["path"].split("?")[0].endswith("/update")
    ]


def _race_notes(detail: dict) -> list[dict]:
    return [
        n for n in note_events(detail) if n.get("delivery") == "push" and n.get("re_land_race")
    ]


def _start_race_story(tmp_path, monkeypatch, server, db_name):
    """Approve a story into `landing` under a live poller; yields
    (client, sid, approved) inside the app's lifetime."""
    import contextlib

    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / db_name))

    @contextlib.contextmanager
    def _ctx():
        with TestClient(create_app(gateway=ScriptedGW())) as client:
            client.patch(
                "/api/v1/settings",
                json={
                    "story_poll_interval": 0.05,
                    "project_root": repo,
                    "write_tools_enabled": True,
                },
            )
            pid = _push_project(client, repo, server, token_file)
            story = client.post(
                "/api/v1/stories", json={"title": "feature", "project_id": pid}
            ).json()
            sid = story["id"]
            run_to_human_review(client, sid)
            approved = client.post(
                f"/api/v1/stories/{sid}/approve", json={"by": "t"}
            ).json()
            assert approved["status"] == "landing", approved
            yield client, sid, approved

    return _ctx()


def test_land_race_updates_the_branch_and_lands_by_itself(tmp_path, monkeypatch):
    """Race + behind: exactly one POST …/update?style=merge, the note says
    attempt 1/3, the story stays landing, and the later merge carries it to
    done — no human ever touched it."""
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}},
        # behind: the merge base of main...head is NOT main's head
        branches={"main": "basehead2"},
        merge_base="basehead1",
        compare_ahead=1,
        # a seeded head: the branch update moves it, so the NEXT poll judges
        # a fresh sha (no statuses) instead of re-reading the same failure
        pull_heads={7: "aaa"},
    )
    try:
        with _start_race_story(tmp_path, monkeypatch, server, "race.db") as (
            client,
            sid,
            approved,
        ):
            # the race signature on the CURRENT head
            server.statuses["aaa"] = _race_statuses()
            # give the poller a moment to run the recovery
            deadline = time.time() + 20.0
            while time.time() < deadline:
                if _update_calls(server):
                    break
                time.sleep(0.05)
            updates = _update_calls(server)
            assert len(updates) == 1, server.requests
            assert (
                updates[0]["path"]
                == "/api/v1/repos/acme/app/pulls/7/update?style=merge"
            )
            assert updates[0]["auth"] == "token sekrit"

            # the story NEVER bounced: still landing, no error
            current = client.get(f"/api/v1/stories/{sid}").json()
            assert current["status"] == "landing", current
            assert current["error"] is None

            # the note names the attempt
            detail = client.get(f"/api/v1/stories/{sid}").json()
            notes = _race_notes(detail)
            assert len(notes) == 1, note_events(detail)
            assert notes[0]["attempt"] == 1
            assert notes[0]["reason"] == "main moved during land, relanding (attempt 1/3)"
            assert notes[0]["pr_index"] == 7

            # the new CI run lands it: the PR merges, the story closes done
            server.pulls[7] = {"merged": True, "mergeable": True, "state": "open"}
            done = wait_status(client, sid, ("done",), timeout=30.0)
            assert done["error"] is None
            assert done["pr_index"] == 7
            assert len(_update_calls(server)) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_land_race_red_test_context_bounces_to_the_gate(tmp_path, monkeypatch):
    """A red test context (no marker anywhere) is not a race: today's bounce,
    zero update calls, no note."""
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}},
        branches={"main": "basehead2"},
        merge_base="basehead1",
        compare_ahead=1,
    )
    try:
        with _start_race_story(tmp_path, monkeypatch, server, "redtest.db") as (
            client,
            sid,
            approved,
        ):
            server.statuses[approved["pushed_sha"]] = {
                "status": "failure",
                "statuses": [
                    {
                        "status": "failure",
                        "context": "ci/acme app/ land (pull_request)",
                        "description": "merge refused: base branch moved",
                    },
                    {
                        "status": "failure",
                        "context": "ci/acme app/ test (pull_request)",
                        "description": "2 tests failed",
                    },
                ],
            }
            gate = wait_status(client, sid, ("human_review",), timeout=30.0)
            assert gate["error"].startswith("land failed:")
            assert _update_calls(server) == []
            assert _race_notes(client.get(f"/api/v1/stories/{sid}").json()) == []
    finally:
        server.shutdown()
        server.server_close()


def test_land_race_land_failure_without_the_marker_bounces(tmp_path, monkeypatch):
    """A land-context failure with no green-marker entry anywhere is not a
    race: gate, zero updates, no note."""
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}},
        branches={"main": "basehead2"},
        merge_base="basehead1",
        compare_ahead=1,
    )
    try:
        with _start_race_story(tmp_path, monkeypatch, server, "nomarker.db") as (
            client,
            sid,
            approved,
        ):
            server.statuses[approved["pushed_sha"]] = {
                "status": "failure",
                "statuses": [
                    {
                        "status": "failure",
                        "context": "ci/acme app/ land (pull_request)",
                        "description": "ci/land: exit 1",
                    },
                    {
                        "status": "success",
                        "context": "ci/acme app/ test (pull_request)",
                        "description": "suite green",
                    },
                ],
            }
            gate = wait_status(client, sid, ("human_review",), timeout=30.0)
            assert gate["error"].startswith("land failed:")
            assert _update_calls(server) == []
            assert _race_notes(client.get(f"/api/v1/stories/{sid}").json()) == []
    finally:
        server.shutdown()
        server.server_close()


def test_land_race_not_behind_bounces_to_the_gate(tmp_path, monkeypatch):
    """Marker + failed land context, but the merge base IS the base head (the
    PR is not behind): not a race we can fix with an update — bounce."""
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}},
        branches={"main": "basehead1"},
        merge_base="basehead1",
        compare_ahead=1,
    )
    try:
        with _start_race_story(tmp_path, monkeypatch, server, "notbehind.db") as (
            client,
            sid,
            approved,
        ):
            server.statuses[approved["pushed_sha"]] = _race_statuses()
            gate = wait_status(client, sid, ("human_review",), timeout=30.0)
            assert gate["error"].startswith("land failed:")
            assert _update_calls(server) == []
            assert _race_notes(client.get(f"/api/v1/stories/{sid}").json()) == []
    finally:
        server.shutdown()
        server.server_close()


def test_land_race_gives_up_after_three_updates(tmp_path, monkeypatch):
    """Three races: exactly 3 update calls, notes 1/3 to 3/3, then the gate
    with `land failed:` — and no fourth call on later ticks."""
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}},
        branches={"main": "basehead2"},
        merge_base="basehead1",
        compare_ahead=1,
        pull_heads={7: "aaa"},
    )
    try:
        with _start_race_story(tmp_path, monkeypatch, server, "three.db") as (
            client,
            sid,
            approved,
        ):
            # the race signature on the head AND every successor (each update
            # moves the head, the next CI run loses the race again)
            server.statuses["aaa"] = _race_statuses()
            server.statuses["aaa-updated"] = _race_statuses()
            server.statuses["aaa-updated-updated"] = _race_statuses()
            server.statuses["aaa-updated-updated-updated"] = _race_statuses()

            gate = wait_status(client, sid, ("human_review",), timeout=60.0)
            assert gate["error"] == (
                "land failed: main moved during land (3 re-land attempts used)"
            )

            # the gate story is watched by the merged-only sweep, so no
            # further update can fire: confirm the count is stable
            deadline = time.time() + 2.0
            while time.time() < deadline:
                time.sleep(0.1)
            updates = _update_calls(server)
            assert len(updates) == 3, [u["path"] for u in server.requests]
            assert all(
                u["path"] == "/api/v1/repos/acme/app/pulls/7/update?style=merge"
                for u in updates
            )

            notes = _race_notes(client.get(f"/api/v1/stories/{sid}").json())
            assert [n["attempt"] for n in notes] == [1, 2, 3], notes
            assert notes[-1]["reason"] == (
                "main moved during land, relanding (attempt 3/3)"
            )
    finally:
        server.shutdown()
        server.server_close()


def test_land_race_update_refused_on_a_merged_pr_closes_done(
    tmp_path, monkeypatch
):
    """The update POST refuses because the PR merged meanwhile: the story is
    simply done, not failed."""
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}},
        branches={"main": "basehead2"},
        merge_base="basehead1",
        compare_ahead=1,
        pull_heads={7: "aaa"},
        # the PR merges between the poller's read and the update POST: the
        # fake answers 422 "already merged" and records the merge, so the
        # re-read the recovery does (the retry-land pattern) sees it
        update_flip_merged=1,
    )
    try:
        with _start_race_story(
            tmp_path, monkeypatch, server, "refused-merged.db"
        ) as (client, sid, approved):
            server.statuses["aaa"] = _race_statuses()
            done = wait_status(client, sid, ("done",), timeout=30.0)
            assert done["error"] is None
            assert done["pr_index"] == 7
            # the refused update DID fire — this is the 422-then-done path
            assert len(_update_calls(server)) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_land_race_update_refused_with_a_real_conflict_parks_at_the_gate(
    tmp_path, monkeypatch
):
    """The update POST refuses with a conflict Gitea cannot auto-fix: the
    story parks at the gate carrying the update error."""
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}},
        branches={"main": "basehead2"},
        merge_base="basehead1",
        compare_ahead=1,
        pull_heads={7: "aaa"},
        update_refusals=99,
    )
    try:
        with _start_race_story(
            tmp_path, monkeypatch, server, "refused-conflict.db"
        ) as (client, sid, approved):
            server.statuses["aaa"] = _race_statuses()
            gate = wait_status(client, sid, ("human_review",), timeout=30.0)
            assert gate["error"].startswith("land failed:")
            assert "cannot update branch" in gate["error"]
            # exactly one attempt was made and no race note was recorded
            assert len(_update_calls(server)) == 1
            assert _race_notes(client.get(f"/api/v1/stories/{sid}").json()) == []
    finally:
        server.shutdown()
        server.server_close()


def test_land_race_proven_by_pr_payload_merge_base_without_compare(
    tmp_path, monkeypatch
):
    """Real Gitea's compare payload has no merge_base: the PR payload's
    merge_base proves the PR is behind, one update, then done."""
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}},
        branches={"main": "basehead2"},
        pr_merge_base="basehead1",
        compare_ahead=1,
        pull_heads={7: "aaa"},
    )
    try:
        with _start_race_story(tmp_path, monkeypatch, server, "prmb.db") as (
            client,
            sid,
            approved,
        ):
            server.statuses["aaa"] = _race_statuses()
            deadline = time.time() + 20.0
            while time.time() < deadline and not _update_calls(server):
                time.sleep(0.05)
            updates = _update_calls(server)
            assert len(updates) == 1, server.requests
            assert (
                updates[0]["path"]
                == "/api/v1/repos/acme/app/pulls/7/update?style=merge"
            )
            notes = _race_notes(client.get(f"/api/v1/stories/{sid}").json())
            assert len(notes) == 1
            assert notes[0]["reason"].startswith("main moved during land, relanding")
            server.pulls[7] = {"merged": True, "mergeable": True, "state": "open"}
            done = wait_status(client, sid, ("done",), timeout=30.0)
            assert done["error"] is None
            assert len(_update_calls(server)) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_land_race_stale_head_is_not_a_verdict_until_the_dwell_runs_out(
    tmp_path, monkeypatch
):
    """The update leaves the head unchanged: the old red status must not
    bounce the story while inside the dwell; past it the gate names why."""
    from app import runner as runner_module

    monkeypatch.setattr(runner_module, "LAND_GREEN_UNMERGED_MIN_DWELL", 1.5)
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}},
        branches={"main": "basehead2"},
        pr_merge_base="basehead1",
        compare_ahead=1,
        pull_heads={7: "aaa"},
        update_keeps_head=True,
    )
    try:
        with _start_race_story(tmp_path, monkeypatch, server, "stale.db") as (
            client,
            sid,
            approved,
        ):
            server.statuses["aaa"] = _race_statuses()
            deadline = time.time() + 20.0
            while time.time() < deadline and not _update_calls(server):
                time.sleep(0.02)
            assert len(_update_calls(server)) == 1
            time.sleep(0.5)
            current = client.get(f"/api/v1/stories/{sid}").json()
            assert current["status"] == "landing", current
            gate = wait_status(client, sid, ("human_review",), timeout=30.0)
            assert gate["error"] == (
                "land failed: main moved during land; "
                "branch update did not move the PR head"
            )
            assert len(_update_calls(server)) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_land_race_counter_survives_a_healthy_poll(tmp_path, monkeypatch):
    """The healthy-poll cleanup must clear only merge_conflict stamps, never
    a land_race counter (the bug to avoid)."""
    from app import runner as runner_module

    assert runner_module._land_race_attempts("land_race:2") == 2
    assert runner_module._land_race_attempts("land_race:0") == 0
    assert runner_module._land_race_attempts("land_race:garbage") == 0
    assert runner_module._land_race_attempts("land_race:2:abc:2026-01-01T00:00:00+00:00") == 2
    assert runner_module._land_race_attempts("merge_conflict:2026-01-01") == 0
    assert runner_module._land_race_attempts(None) == 0
    assert runner_module._land_race_attempts("") == 0
    assert runner_module.LAND_RACE_MAX_ATTEMPTS == 3


# ---- land run green but the PR never merged ---------------------------------


def _green_statuses():
    """The combined view of a head whose land job went green: exactly
    "success" — the marker status is posted BEFORE the merge POST, so a
    refused merge still leaves this signature."""
    return {
        "status": "success",
        "statuses": [
            {
                "status": "success",
                "context": "ci/acme app/ test (pull_request)",
                "description": "suite green on the merge result",
            }
        ],
    }


def _fast_land_polls(monkeypatch, dwell=0.3):
    """Tight poll spacing and dwell so the recovery runs in milliseconds.

    Both constants are module globals read at call time inside the poller,
    so patching the attributes reaches the running loop (the env vars are
    only read at import, long before the test)."""
    from app import runner as runner_module

    monkeypatch.setattr(runner_module, "LAND_POLL_MIN_INTERVAL", 0.05)
    monkeypatch.setattr(
        runner_module, "LAND_GREEN_UNMERGED_MIN_DWELL", float(dwell)
    )


def _ci_pr_notes(detail: dict) -> list[dict]:
    return [
        n for n in note_events(detail) if n.get("delivery") == "push" and n.get("ci_pr_green")
    ]


def _green_notes(detail: dict) -> list[dict]:
    return [
        n
        for n in note_events(detail)
        if n.get("delivery") == "push" and n.get("re_land_green")
    ]


def test_land_green_unmerged_re_lands_after_the_dwell(tmp_path, monkeypatch):
    """Green suite, open PR, dwell elapsed: exactly one POST
    …/update?style=merge, the note says attempt 1/3, the story stays
    landing (incident run 3053 / PR 70, parked 7h)."""
    _fast_land_polls(monkeypatch)
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}},
        pull_heads={7: "aaa"},
        pull_files={7: ["backend/app/runner.py"]},
    )
    try:
        with _start_race_story(
            tmp_path, monkeypatch, server, "green-unmerged.db"
        ) as (client, sid, approved):
            server.statuses["aaa"] = _green_statuses()

            deadline = time.time() + 20.0
            while time.time() < deadline:
                if _update_calls(server):
                    break
                time.sleep(0.05)
            updates = _update_calls(server)
            assert len(updates) == 1, server.requests
            assert (
                updates[0]["path"]
                == "/api/v1/repos/acme/app/pulls/7/update?style=merge"
            )
            assert updates[0]["auth"] == "token sekrit"

            current = client.get(f"/api/v1/stories/{sid}").json()
            assert current["status"] == "landing", current
            assert current["error"] is None

            notes = _green_notes(current)
            assert len(notes) == 1, note_events(current)
            assert notes[0]["attempt"] == 1
            assert notes[0]["reason"] == (
                "land run green but the PR is not merged (attempt 1/3)"
            )
            assert notes[0]["pr_index"] == 7
    finally:
        server.shutdown()
        server.server_close()


def test_land_green_merged_pr_closes_done_with_zero_updates(tmp_path, monkeypatch):
    """A merged PR outranks the green-unmerged signal (the merged check runs
    first): done, and no branch update was ever attempted."""
    _fast_land_polls(monkeypatch)
    server = _fake_gitea(
        pulls={7: {"merged": True, "mergeable": True, "state": "open"}},
        pull_heads={7: "aaa"},
        pull_files={7: ["backend/app/runner.py"]},
    )
    try:
        with _start_race_story(tmp_path, monkeypatch, server, "green-done.db") as (
            client,
            sid,
            approved,
        ):
            server.statuses["aaa"] = _green_statuses()
            done = wait_status(client, sid, ("done",), timeout=30.0)
            assert done["error"] is None
            assert done["pr_index"] == 7
            assert _update_calls(server) == []
    finally:
        server.shutdown()
        server.server_close()


def test_land_green_in_progress_merge_inside_the_dwell_never_updates(
    tmp_path, monkeypatch
):
    """Green+unmerged observed well INSIDE the dwell, then the PR merges:
    done with ZERO update calls — the dwell is what keeps a landing human
    merge from being fought by the safety net."""
    # a dwell far longer than the poll spacing: the negative window below
    # is honest (a dropped dwell condition would fire inside it)
    _fast_land_polls(monkeypatch, dwell=5.0)
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}},
        pull_heads={7: "aaa"},
        pull_files={7: ["backend/app/runner.py"]},
    )
    try:
        with _start_race_story(
            tmp_path, monkeypatch, server, "green-inflight.db"
        ) as (client, sid, approved):
            server.statuses["aaa"] = _green_statuses()

            # several sweeps happen inside the dwell (poll spacing 0.05s):
            # none of them may touch the branch
            deadline = time.time() + 1.5
            while time.time() < deadline:
                assert _update_calls(server) == [], server.requests
                current = client.get(f"/api/v1/stories/{sid}").json()
                assert current["status"] == "landing", current
                time.sleep(0.05)
            assert _update_calls(server) == [], server.requests

            # the human merge lands well inside the dwell
            server.pulls[7] = {"merged": True, "mergeable": True, "state": "open"}
            done = wait_status(client, sid, ("done",), timeout=30.0)
            assert done["error"] is None
            assert _update_calls(server) == [], server.requests
    finally:
        server.shutdown()
        server.server_close()


def test_land_green_pending_status_never_triggers_a_re_land(tmp_path, monkeypatch):
    """Only the literal "success" qualifies: a pending combined status (CI
    still running) leaves the story landing with zero update calls."""
    _fast_land_polls(monkeypatch)
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}},
        pull_heads={7: "aaa"},
        pull_files={7: ["backend/app/runner.py"]},
    )
    try:
        with _start_race_story(
            tmp_path, monkeypatch, server, "green-pending.db"
        ) as (client, sid, approved):
            server.statuses["aaa"] = {
                "status": "pending",
                "statuses": [
                    {
                        "status": "pending",
                        "context": "ci/acme app/ test (pull_request)",
                        "description": "suite running",
                    }
                ],
            }
            deadline = time.time() + 1.5
            while time.time() < deadline:
                assert _update_calls(server) == [], server.requests
                current = client.get(f"/api/v1/stories/{sid}").json()
                assert current["status"] == "landing", current
                time.sleep(0.05)
            assert _update_calls(server) == []
            detail = client.get(f"/api/v1/stories/{sid}").json()
            assert _green_notes(detail) == []
    finally:
        server.shutdown()
        server.server_close()


def test_land_green_ci_pr_waits_for_the_coordinator(tmp_path, monkeypatch):
    """A CI PR (files under .gitea/) is green-and-unmerged on purpose: zero
    update calls, exactly ONE explanatory note, the story stays landing."""
    _fast_land_polls(monkeypatch)
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}},
        pull_heads={7: "aaa"},
        pull_files={7: [".gitea/workflows/ci.yml", "README.md"]},
    )
    try:
        with _start_race_story(
            tmp_path, monkeypatch, server, "green-cipr.db"
        ) as (client, sid, approved):
            server.statuses["aaa"] = _green_statuses()

            # the note appears once the dwell has passed
            deadline = time.time() + 20.0
            while time.time() < deadline:
                if _ci_pr_notes(client.get(f"/api/v1/stories/{sid}").json()):
                    break
                time.sleep(0.05)

            # idempotent: later sweeps never re-note and never re-land
            deadline = time.time() + 1.0
            while time.time() < deadline:
                time.sleep(0.05)
            detail = client.get(f"/api/v1/stories/{sid}").json()
            notes = _ci_pr_notes(detail)
            assert len(notes) == 1, note_events(detail)
            assert notes[0]["reason"] == (
                "CI PR green, waiting for the coordinator's force-merge"
            )
            assert notes[0]["pr_index"] == 7
            assert _update_calls(server) == [], server.requests
            assert _green_notes(detail) == []
            assert detail["status"] == "landing", detail
    finally:
        server.shutdown()
        server.server_close()


# ---- pr_web_base: public PR links -------------------------------------------


def test_public_pr_url_branches():
    from app.pr import public_pr_url

    kw = {"api_base": "http://gitea.svc:3000/", "repo": "acme/app", "index": 7}
    internal = "http://gitea.gitea.svc.cluster.local:3000/acme/app/pulls/7"
    # web_base set, html_url present: re-hosted; port and trailing slash kept
    assert (
        public_pr_url(internal, web_base="https://git.hoeve.nu", **kw)
        == "https://git.hoeve.nu/acme/app/pulls/7"
    )
    assert (
        public_pr_url(internal + "?x=1", web_base="https://git.hoeve.nu:8443/", **kw)
        == "https://git.hoeve.nu:8443/acme/app/pulls/7?x=1"
    )
    # web_base set, no html_url
    assert (
        public_pr_url(None, web_base="https://git.hoeve.nu/", **kw)
        == "https://git.hoeve.nu/acme/app/pulls/7"
    )
    # web_base unset: unchanged, or built from api_base
    assert public_pr_url(internal, web_base=None, **kw) == internal
    assert (
        public_pr_url(None, web_base=None, **kw)
        == "http://gitea.svc:3000/acme/app/pulls/7"
    )


def _approve_with_web_base(tmp_path, monkeypatch, web_base, **fake):
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(**fake)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "web.db"))
    try:
        with TestClient(create_app(gateway=ScriptedGW())) as client:
            client.patch(
                "/api/v1/settings",
                json={
                    "story_poll_interval": 0.05,
                    "project_root": repo,
                    "write_tools_enabled": True,
                },
            )
            body = {
                "name": "p",
                "root": repo,
                "delivery": "push",
                "pr_api_base": f"http://127.0.0.1:{server.server_address[1]}",
                "pr_repo": "acme/app",
                "pr_base_branch": "main",
                "pr_token_file": str(token_file),
            }
            if web_base:
                body["pr_web_base"] = web_base
            project = client.post("/api/v1/projects", json=body).json()
            story = client.post(
                "/api/v1/stories",
                json={"title": "feature", "project_id": project["id"]},
            ).json()
            run_to_human_review(client, story["id"])
            final = client.post(
                f"/api/v1/stories/{story['id']}/approve", json={"by": "t"}
            ).json()
            posts = [r for r in server.requests if r["method"] == "POST"]
            return final, posts
    finally:
        server.shutdown()
        server.server_close()


def test_internal_html_url_is_rehosted_on_pr_web_base(tmp_path, monkeypatch):
    final, posts = _approve_with_web_base(
        tmp_path,
        monkeypatch,
        "https://git.hoeve.nu",
        pr_html_url="http://gitea.gitea.svc.cluster.local:3000/acme/app/pulls/7",
    )
    assert final["status"] == "landing", final
    assert final["pr_url"] == "https://git.hoeve.nu/acme/app/pulls/7"
    assert [p["path"] for p in posts] == ["/api/v1/repos/acme/app/pulls"]


def test_html_url_is_stored_unchanged_without_pr_web_base(tmp_path, monkeypatch):
    internal = "http://gitea.gitea.svc.cluster.local:3000/acme/app/pulls/7"
    final, _ = _approve_with_web_base(
        tmp_path, monkeypatch, None, pr_html_url=internal
    )
    assert final["pr_url"] == internal


def test_missing_html_url_is_built_from_pr_web_base(tmp_path, monkeypatch):
    final, _ = _approve_with_web_base(
        tmp_path, monkeypatch, "https://git.hoeve.nu", pr_html_url=None
    )
    assert final["status"] == "landing", final
    assert final["pr_url"] == "https://git.hoeve.nu/acme/app/pulls/7"


def test_pr_web_base_round_trips_through_patch(tmp_path, monkeypatch):
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "rt.db"))
    with TestClient(create_app(gateway=ScriptedGW())) as client:
        pid = client.post(
            "/api/v1/projects", json={"name": "p", "root": str(tmp_path)}
        ).json()["id"]
        def listed():
            rows = client.get("/api/v1/projects").json()
            return next(r for r in rows if r["id"] == pid)["pr_web_base"]

        assert listed() is None
        res = client.patch(
            f"/api/v1/projects/{pid}", json={"pr_web_base": "https://git.hoeve.nu"}
        )
        assert res.json()["pr_web_base"] == "https://git.hoeve.nu"
        assert listed() == "https://git.hoeve.nu"
        res = client.patch(f"/api/v1/projects/{pid}", json={"pr_web_base": ""})
        assert res.json()["pr_web_base"] is None
