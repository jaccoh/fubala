"""The failed lane holds only actionable failures.

Three healers, one contract:
  - a transient transport blip during the landing step is retried (bounded),
    so a blip never strands a story in `failed`;
  - a failed push+PR story whose branch actually merged self-heals to done
    (the failure was an artefact — the work landed);
  - an operator can close a failed story as `superseded`: terminal, in its
    own collapsed lane, away from the real failures.
"""

import json
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import pr as pr_module
from app import runner as runner_module
from app import workspaces
from app.main import create_app

from test_delivery_push import (
    ScriptedGW,
    _fake_gitea,
    _push_project,
    init_remote,
    init_repo,
    note_events,
    run_to_human_review,
    wait_status,
)


@pytest.fixture(autouse=True)
def _fast_sweeps(monkeypatch):
    """The land poller / reconcile sweep spacing would starve these tests."""
    monkeypatch.setattr(runner_module, "LAND_POLL_MIN_INTERVAL", 0.0)


@pytest.fixture(autouse=True)
def _fast_pr_retry(monkeypatch):
    """No real backoff sleeps inside the synchronous approve."""
    monkeypatch.setattr(pr_module, "PR_BACKOFF_BASE", 0.0)
    monkeypatch.setattr(pr_module, "PR_RETRY_WINDOW", 1.0)
    monkeypatch.setattr(pr_module, "PR_MAX_ATTEMPTS", 3)


def _git_out(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _serve(handler_cls) -> object:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def _shutdown(server) -> None:
    server.shutdown()
    server.server_close()


def _respond(handler, code: int, payload) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _force_story_row(db_path, *, story_id=None, **columns) -> str:
    """Mutate a story row directly while the app runs, so a test can stage
    any failure shape (the runner reads the row on every sweep)."""
    conn = sqlite3.connect(db_path)
    try:
        if story_id is None:
            story_id = conn.execute("SELECT id FROM stories").fetchall()[-1][0]
        sets, params = [], []
        for column, value in columns.items():
            sets.append(f"{column}=?")
            params.append(value)
        if sets:
            params.append(story_id)
            conn.execute(
                f"UPDATE stories SET {', '.join(sets)} WHERE id=?", params
            )
            conn.commit()
    finally:
        conn.close()
    return story_id


# ---- pr module: bounded retry + compare/branch helpers ---------------------


def test_pr_transient_retry_then_success_and_exhaustion():
    calls = {"n": 0}

    class Flaky(BaseHTTPRequestHandler):
        """503 twice, then a real PR — a landing blip that heals itself."""

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            calls["n"] += 1
            if calls["n"] <= 2:
                _respond(self, 503, {"message": "warming up"})
                return
            _respond(
                self,
                201,
                {"html_url": "http://gitea.local/acme/app/pulls/7", "number": 7},
            )

        def log_message(self, *args):
            pass

    server = _serve(Flaky)
    try:
        url, index = pr_module.create_pull(
            base_url=_url(server),
            repo="acme/app",
            token="sekrit",
            head="fubala/x",
            base_branch="main",
            title="t",
        )
        assert url.endswith("/pulls/7") and index == 7
        assert calls["n"] == 3  # two blips absorbed
    finally:
        _shutdown(server)

    calls["n"] = 0

    class AlwaysDown(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            calls["n"] += 1
            _respond(self, 503, {"message": "warming up"})

        def log_message(self, *args):
            pass

    server = _serve(AlwaysDown)
    try:
        with pytest.raises(pr_module.PullRequestError) as exc:
            pr_module.create_pull(
                base_url=_url(server),
                repo="acme/app",
                token="sekrit",
                head="fubala/x",
                base_branch="main",
                title="t",
            )
        assert "transient network error (3 attempts)" in str(exc.value)
        assert not isinstance(exc.value, pr_module.TransientPullRequestError)
        assert calls["n"] == 3
    finally:
        _shutdown(server)


def test_pr_4xx_is_never_retried():
    calls = {"n": 0}

    class Rejects(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            calls["n"] += 1
            _respond(self, 422, {"message": "bad payload"})

        def log_message(self, *args):
            pass

    server = _serve(Rejects)
    try:
        with pytest.raises(pr_module.PullRequestError) as exc:
            pr_module.create_pull(
                base_url=_url(server),
                repo="acme/app",
                token="sekrit",
                head="fubala/x",
                base_branch="main",
                title="t",
            )
        assert "422" in str(exc.value)
        assert "transient network error" not in str(exc.value)
        assert calls["n"] == 1
    finally:
        _shutdown(server)


def test_pr_branch_head_and_branch_merged():
    state = {
        "ahead": 0,
        "branch": "feedbeef",
        "pull": {
            "merged": True,
            "mergeable": True,
            "state": "open",
            "merge_commit_sha": "abc123",
        },
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.rstrip("/")
            if "/compare/" in path:
                ahead = state["ahead"]
                if ahead is None:
                    _respond(self, 404, {"message": "unknown base"})
                    return
                commits = [{"sha": f"c{i}"} for i in range(ahead)]
                _respond(self, 200, {"total_commits": ahead, "commits": commits})
                return
            if "/branches/" in path:
                if state["branch"] is None:
                    _respond(self, 404, {"message": "branch not found"})
                    return
                _respond(self, 200, {"name": "b", "commit": {"id": state["branch"]}})
                return
            if "/pulls/" in path:
                if state["pull"] is None:
                    _respond(self, 404, {"message": "pull not found"})
                    return
                _respond(self, 200, {"number": 7, **state["pull"]})
                return
            _respond(self, 404, {"message": "nope"})

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    try:
        assert (
            pr_module.branch_head(_url(server), "acme/app", "sekrit", "fubala/x")
            == "feedbeef"
        )
        contained, sha = pr_module.branch_merged(
            _url(server), "acme/app", "sekrit", "deadbeef", "main", pr_index=7
        )
        assert contained is True
        assert sha == "abc123"

        # a branch still carrying unmerged work is not contained
        state["ahead"] = 2
        contained, sha = pr_module.branch_merged(
            _url(server), "acme/app", "sekrit", "deadbeef", "main", pr_index=7
        )
        assert contained is False
        assert sha is None

        # deleted branch / unknown compare: a skip, not a crash
        state["ahead"] = None
        with pytest.raises(pr_module.PullRequestError):
            pr_module.branch_merged(
                _url(server), "acme/app", "sekrit", "deadbeef", "main"
            )
        state["branch"] = None
        assert (
            pr_module.branch_head(_url(server), "acme/app", "sekrit", "fubala/x")
            is None
        )

        # no PR to name a merge commit: contained, sha None
        state["ahead"] = 0
        contained, sha = pr_module.branch_merged(
            _url(server), "acme/app", "sekrit", "deadbeef", "main"
        )
        assert contained is True and sha is None
    finally:
        _shutdown(server)


# ---- git push retry --------------------------------------------------------


def test_push_retries_transient_transport_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(workspaces, "PUSH_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(workspaces, "PUSH_BACKOFF_BASE", 0.0)
    monkeypatch.setattr(workspaces, "PUSH_RETRY_WINDOW", 1.0)

    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    worktrees = {}
    for name in ("story1", "story2", "story3"):
        wt, _base, _new = workspaces.create(repo, name)
        (wt / "feature.py").write_text(f"VALUE = '{name}'\n")
        worktrees[name] = wt

    real_git = workspaces._git
    attempts = {"n": 0}

    def flaky_push(project_root, *args, **kwargs):
        if args and args[0] == "push":
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise workspaces.WorkspaceError(
                    "fatal: unable to access 'http://x/': Failed to connect to x"
                )
        return real_git(project_root, *args, **kwargs)

    monkeypatch.setattr(workspaces, "_git", flaky_push)
    assert workspaces.push(repo, "story1")
    assert attempts["n"] == 2  # one blip absorbed

    attempts["n"] = 0

    def dead_push(project_root, *args, **kwargs):
        if args and args[0] == "push":
            attempts["n"] += 1
            raise workspaces.WorkspaceError(
                "fatal: unable to access 'http://x/': Could not resolve host x"
            )
        return real_git(project_root, *args, **kwargs)

    monkeypatch.setattr(workspaces, "_git", dead_push)
    with pytest.raises(workspaces.WorkspaceError) as exc:
        workspaces.push(repo, "story2")
    assert "transient network error (3)" in str(exc.value)
    assert attempts["n"] == 3

    # a NON-transient git failure (rejected refspec) is never retried
    attempts["n"] = 0

    def refused(project_root, *args, **kwargs):
        if args and args[0] == "push":
            attempts["n"] += 1
            raise workspaces.WorkspaceError(
                "! [remote rejected] fubala/story3 -> main (permission denied)"
            )
        return real_git(project_root, *args, **kwargs)

    monkeypatch.setattr(workspaces, "_git", refused)
    with pytest.raises(workspaces.WorkspaceError) as exc:
        workspaces.push(repo, "story3")
    assert "transient network error" not in str(exc.value)
    assert attempts["n"] == 1


# ---- reconcile sweep -------------------------------------------------------


def _fail_landing(client, pid: str) -> str:
    """Queue → … → human_review → approve, expecting the landing to fail."""
    story = client.post(
        "/api/v1/stories", json={"title": "feature", "project_id": pid}
    ).json()
    sid = story["id"]
    run_to_human_review(client, sid)
    client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
    return sid


def test_transient_blip_then_success_lands_not_failed(tmp_path, monkeypatch):
    """AC: PR create failing transiently N-1 times then succeeding leaves the
    story in `landing`, never `failed`."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}},
        pr_create_failures=2,
    )
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "blip.db"))
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
            sid = _fail_landing(client, pid)
            landed = wait_status(client, sid, ("landing", "failed"))
            assert landed["status"] == "landing", landed.get("error")
            assert landed["pr_index"] == 7
            posts = [
                r
                for r in server.requests
                if r["method"] == "POST" and r["path"].endswith("/pulls")
            ]
            assert len(posts) == 3, server.requests
    finally:
        _shutdown(server)


def test_exhausted_retries_fail_with_transient_prefix(tmp_path, monkeypatch):
    """AC: all attempts exhausted ⇒ failed with `transient network error`."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(pr_create_failures=99)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "exhaust.db"))
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
            sid = _fail_landing(client, pid)
            failed = wait_status(client, sid, ("failed",))
            assert "transient network error (3 attempts)" in (failed["error"] or "")
            notes = [
                n
                for n in note_events(client.get(f"/api/v1/stories/{sid}").json())
                if n.get("delivery") == "push"
            ]
            assert notes and notes[-1].get("error")
            assert notes[-1].get("pushed_sha")  # the push itself succeeded
            assert failed["pushed_sha"] == notes[-1]["pushed_sha"]
    finally:
        _shutdown(server)


def test_reconcile_merged_failed_story_self_heals_to_done(tmp_path, monkeypatch):
    """AC: failed + own commits + merged branch ⇒ done, `land_reconciled`
    event naming the merge commit, error cleared."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(
        pr_create_failures=99,
        pulls={
            7: {
                "merged": True,
                "mergeable": True,
                "state": "open",
                "merge_commit_sha": "abc123",
            }
        },
        compare_ahead=0,
    )
    db_path = tmp_path / "reconcile.db"
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
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
            sid = _fail_landing(client, pid)
            failed = wait_status(client, sid, ("failed",))
            assert failed["pushed_sha"]
            assert failed["pushed_sha"] != _git_out(repo, "rev-parse", "main")

            # the operator merged the branch by hand and the PR was recorded
            _force_story_row(db_path, story_id=sid, pr_index=7)
            server.branches[f"fubala/{sid}"] = failed["pushed_sha"]

            done = wait_status(client, sid, ("done",), timeout=30.0)
            assert done["error"] is None
            detail = client.get(f"/api/v1/stories/{sid}").json()
            events = [
                json.loads(e.get("data") or "{}")
                for e in detail.get("events", [])
                if e.get("kind") == "land_reconciled"
            ]
            assert events, f"no land_reconciled event: {detail.get('events')}"
            assert events[-1]["merge_sha"] == "abc123"
            assert events[-1]["pushed_sha"] == failed["pushed_sha"]
    finally:
        _shutdown(server)


def test_reconcile_not_merged_stays_failed(tmp_path, monkeypatch):
    """AC: an unmerged branch never self-heals."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(pr_create_failures=99, compare_ahead=3)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "notmerged.db"))
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
            sid = _fail_landing(client, pid)
            failed = wait_status(client, sid, ("failed",))
            server.branches[f"fubala/{sid}"] = failed["pushed_sha"]
            time.sleep(0.8)
            still = client.get(f"/api/v1/stories/{sid}").json()
            assert still["status"] == "failed", still
            assert still["error"]
    finally:
        _shutdown(server)


def test_reconcile_skips_no_changes_story(tmp_path, monkeypatch):
    """AC: branch head == base_ref (a story that produced no commit) stays
    failed — "0 ahead" proves nothing without a commit of its own."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(pr_create_failures=99, compare_ahead=0)
    db_path = tmp_path / "nochanges.db"
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
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
            sid = _fail_landing(client, pid)
            wait_status(client, sid, ("failed",))
            base = _git_out(repo, "rev-parse", "main")
            _force_story_row(db_path, story_id=sid, base_ref=base, pushed_sha=base)
            server.branches[f"fubala/{sid}"] = base
            time.sleep(0.8)
            still = client.get(f"/api/v1/stories/{sid}").json()
            assert still["status"] == "failed", still
    finally:
        _shutdown(server)


def test_reconcile_skips_coder_failures(tmp_path, monkeypatch):
    """AC: a coder/verify failure whose branch happens to be contained stays
    failed — only landing-step failures reconcile."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(compare_ahead=0)
    db_path = tmp_path / "coderfail.db"
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
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
            # coder/verify-shaped failure: no pr_index, no `land failed:`
            # prefix, no delivery-push error note
            _force_story_row(
                db_path,
                story_id=sid,
                status="failed",
                error="coder produced no changes",
                base_ref=_git_out(repo, "rev-parse", "main"),
                pushed_sha="0" * 40,
            )
            server.branches[f"fubala/{sid}"] = "0" * 40
            time.sleep(0.8)
            still = client.get(f"/api/v1/stories/{sid}").json()
            assert still["status"] == "failed", still
            assert still["error"] == "coder produced no changes"
    finally:
        _shutdown(server)


def test_reconcile_skips_merge_mode_and_external_stories(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(compare_ahead=0)
    db_path = tmp_path / "gates.db"
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
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
            # merge-mode project: no pr_* config at all
            merge_project = client.post(
                "/api/v1/projects", json={"name": "merge-p", "root": repo}
            ).json()
            story = client.post(
                "/api/v1/stories",
                json={"title": "feature", "project_id": merge_project["id"]},
            ).json()
            sid = story["id"]
            run_to_human_review(client, sid)
            _force_story_row(
                db_path,
                story_id=sid,
                status="failed",
                error="land failed: merge conflict",
                base_ref=_git_out(repo, "rev-parse", "main"),
                pushed_sha="0" * 40,
            )
            server.branches[f"fubala/{sid}"] = "0" * 40
            time.sleep(0.8)
            assert (
                client.get(f"/api/v1/stories/{sid}").json()["status"] == "failed"
            )

            # external story in a push project: owned by its claimer
            pid = _push_project(client, repo, server, token_file)
            external = client.post(
                "/api/v1/stories",
                json={
                    "title": "ext",
                    "project_id": pid,
                    "execution_override": "external",
                },
            ).json()
            _force_story_row(
                db_path,
                story_id=external["id"],
                status="failed",
                error="land failed: remote unreachable",
                base_ref=_git_out(repo, "rev-parse", "main"),
                pushed_sha="0" * 40,
            )
            server.branches[f"fubala/{external['id']}"] = "0" * 40
            time.sleep(0.8)
            assert (
                client.get(f"/api/v1/stories/{external['id']}").json()["status"]
                == "failed"
            )
    finally:
        _shutdown(server)


def test_reconcile_skips_deleted_branch(tmp_path, monkeypatch):
    """A 404 on the compare/branch endpoint means "cannot prove" — skip."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(pr_create_failures=99, compare_ahead=None)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "deleted.db"))
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
            sid = _fail_landing(client, pid)
            failed = wait_status(client, sid, ("failed",))
            # head unknown on the remote AND pushed_sha wiped (legacy row)
            _force_story_row(
                tmp_path / "deleted.db", story_id=sid, pushed_sha=None
            )
            assert failed["status"] == "failed"
            time.sleep(0.8)
            still = client.get(f"/api/v1/stories/{sid}").json()
            assert still["status"] == "failed", still
    finally:
        _shutdown(server)


# ---- supersede -------------------------------------------------------------


def _gate_story_with_open_pr(client, pid: str, sid: str) -> dict:
    """Drive a push+PR story to the gate WITH an open PR: approve once
    (push + PR open, landing), then break the project's Gitea config so the
    poller parks the story back at human_review carrying the PR facts."""
    run_to_human_review(client, sid)
    approved = client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"}).json()
    assert approved["status"] == "landing", approved
    client.patch(f"/api/v1/projects/{pid}", json={"pr_api_base": None})
    gate = wait_status(client, sid, ("human_review",), timeout=30.0)
    assert gate["pr_index"] == 7, gate
    return gate


def test_supersede_human_review_story_cleans_up_pr_and_worktree(
    tmp_path, monkeypatch
):
    """AC: a story waiting at the gate (worktree + pushed branch + open PR)
    is superseded with 200, its PR is commented (`superseded_by` named) and
    closed, and the worktree is discarded."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}}
    )
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "supgate.db"))
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
            gate = _gate_story_with_open_pr(client, pid, sid)
            assert gate["status"] == "human_review", gate.get("error")
            # the worktree was already discarded when the PR opened (the
            # branch lives on the remote); supersede's teardown below is
            # about the branch remnant, not a live checkout
            assert not (Path(repo) / ".fubala-worktrees" / sid).exists()

            # the poller needs the config back for supersede to reach Gitea
            client.patch(
                f"/api/v1/projects/{pid}",
                json={"pr_api_base": f"http://127.0.0.1:{server.server_address[1]}"},
            )
            closed = client.post(
                f"/api/v1/stories/{sid}/supersede",
                json={"by": "human", "superseded_by": "fubala/42"},
            )
            assert closed.status_code == 200, closed.text
            assert closed.json()["status"] == "superseded"

            # best-effort teardown actually happened
            assert not (Path(repo) / ".fubala-worktrees" / sid).exists()
            pr_calls = [
                r
                for r in server.requests
                # the land poller's GET /pulls/7 is not a supersede call
                if (r["method"] == "POST" and r["path"].endswith("/comments"))
                or (r["method"] == "PATCH" and r["path"].endswith("/pulls/7"))
            ]
            assert len(pr_calls) == 2, server.requests
            assert pr_calls[0]["method"] == "POST"
            assert pr_calls[0]["body"]["body"] and (
                "fubala/42" in pr_calls[0]["body"]["body"]
            )
            assert pr_calls[1]["method"] == "PATCH"
            assert pr_calls[1]["body"] == {"state": "closed"}
            assert server.pulls[7]["state"] == "closed"

            detail = client.get(f"/api/v1/stories/{sid}").json()
            notes = [n for n in note_events(detail) if n.get("superseded")]
            assert notes, detail.get("events")
            assert notes[-1]["by"] == "human"
            assert notes[-1]["superseded_by"] == "fubala/42"
            assert notes[-1]["pr_closed"] is True
    finally:
        _shutdown(server)


def test_supersede_without_pr_config_makes_no_remote_calls(tmp_path, monkeypatch):
    """AC: no pr_index / no PR config on the project ⇒ no Gitea calls at all
    (the worktree is still discarded)."""
    repo = init_repo(tmp_path)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea()
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "supnopr.db"))
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
                "/api/v1/stories", json={"title": "plain", "project_id": pid}
            ).json()
            sid = story["id"]
            gate = run_to_human_review(client, sid)
            assert gate["status"] == "human_review", gate.get("error")

            closed = client.post(
                f"/api/v1/stories/{sid}/supersede", json={"by": "op"}
            )
            assert closed.status_code == 200, closed.text
            assert closed.json()["status"] == "superseded"
            assert closed.json()["pr_index"] is None

            # push-only delivery (no pr_* config): zero remote calls
            assert not [r for r in server.requests if r["method"] in ("POST", "PATCH")], server.requests
            assert not (Path(repo) / ".fubala-worktrees" / sid).exists()
    finally:
        _shutdown(server)


def test_supersede_merged_pr_close_failure_never_fails_the_close(tmp_path, monkeypatch):
    """AC: Gitea unreachable at supersede time ⇒ still 200 superseded; the
    PR error is recorded in the trailing note (pr_closed: false), never
    raised."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}}
    )
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "supdown.db"))
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
            _gate_story_with_open_pr(client, pid, sid)
            gate = client.get(f"/api/v1/stories/{sid}").json()
            assert gate["status"] == "human_review", gate.get("error")
            assert gate["pr_index"] == 7

            # the poller park nulls the project's pr_api_base (that's why the
            # story is at the gate); restore it so supersede actually has a
            # remote to fail against, THEN kill the server: the transition
            # must survive the outage, not silently skip the remote.
            client.patch(
                f"/api/v1/projects/{pid}",
                json={"pr_api_base": f"http://127.0.0.1:{server.server_address[1]}"},
            )
            _shutdown(server)  # Gitea dies before the operator supersedes

            closed = client.post(
                f"/api/v1/stories/{sid}/supersede",
                json={"by": "human", "superseded_by": "hand-landed"},
            )
            assert closed.status_code == 200, closed.text
            assert closed.json()["status"] == "superseded"

            detail = client.get(f"/api/v1/stories/{sid}").json()
            notes = [n for n in note_events(detail) if n.get("superseded")]
            assert notes[-1]["pr_closed"] is False
            assert notes[-1]["pr_error"]
            assert "sekrit" not in json.dumps(detail)
    finally:
        _shutdown(server)  # double close is fine


def test_supersede_running_statuses_are_refused(tmp_path, monkeypatch):
    """AC: landing → 409 with the story untouched; in_progress → 409 whose
    detail says to cancel first."""
    repo = init_repo(tmp_path)
    db_path = tmp_path / "suprun.db"
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
    with TestClient(create_app(gateway=ScriptedGW())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "wip"}).json()
        sid = story["id"]

        # external stories are owned by whoever claimed them: the scheduler
        # never picks them up, not even as restart orphans, so the row keeps
        # the status we forced until the endpoint under test sees it (an
        # internal row with no project override is adopted as an orphan,
        # flipped to executing, and races the 409 we assert below)
        _force_story_row(
            db_path, story_id=sid, status="landing", execution="external"
        )
        refused = client.post(f"/api/v1/stories/{sid}/supersede", json={"by": "t"})
        assert refused.status_code == 409
        assert (
            client.get(f"/api/v1/stories/{sid}").json()["status"] == "landing"
        )

        _force_story_row(
            db_path, story_id=sid, status="in_progress", execution="external"
        )
        refused = client.post(f"/api/v1/stories/{sid}/supersede", json={"by": "t"})
        assert refused.status_code == 409
        assert "cancel the story first" in refused.json()["detail"]
        assert (
            client.get(f"/api/v1/stories/{sid}").json()["status"] == "in_progress"
        )


def test_supersede_endpoint_and_lane_split(tmp_path, monkeypatch):
    """AC: POST /supersede on failed ⇒ superseded + event; 409 otherwise;
    the failed lane excludes it."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(pr_create_failures=99)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "supersede.db"))
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
            sid = _fail_landing(client, pid)
            wait_status(client, sid, ("failed",))

            closed = client.post(
                f"/api/v1/stories/{sid}/supersede",
                json={"by": "human", "superseded_by": "landed by hand in #123"},
            )
            assert closed.status_code == 200, closed.text
            body = closed.json()
            assert body["status"] == "superseded"
            assert body["error"] is None

            detail = client.get(f"/api/v1/stories/{sid}").json()
            notes = [n for n in note_events(detail) if n.get("superseded")]
            assert notes, detail.get("events")
            assert notes[-1]["superseded_by"] == "landed by hand in #123"
            assert notes[-1]["by"] == "human"

            # terminal: no retry, no re-supersede, no supersede of non-failed
            assert (
                client.post(
                    f"/api/v1/stories/{sid}/retry",
                    json={"stage": "queued", "by": "t"},
                ).status_code
                == 409
            )
            assert (
                client.post(
                    f"/api/v1/stories/{sid}/supersede", json={"by": "t"}
                ).status_code
                == 409
            )
            other = client.post(
                "/api/v1/stories", json={"title": "other", "project_id": pid}
            ).json()
            refused = client.post(
                f"/api/v1/stories/{other['id']}/supersede", json={"by": "t"}
            )
            assert refused.status_code == 409
            assert "only failed or human-review stories" in refused.json()["detail"]

            # the failed lane is empty: the closed story lives apart from it
            listed = client.get("/api/v1/stories").json()
            assert [s["status"] for s in listed if s["id"] == sid] == ["superseded"]
            assert not [s for s in listed if s["status"] == "failed"]
    finally:
        _shutdown(server)


def test_superseded_story_is_never_picked_up(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    db_path = tmp_path / "terminal.db"
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
    with TestClient(create_app(gateway=ScriptedGW())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "x"}).json()
        sid = story["id"]
        _force_story_row(db_path, story_id=sid, status="superseded")
        time.sleep(0.6)
        current = client.get(f"/api/v1/stories/{sid}").json()
        assert current["status"] == "superseded", current
        # no pipeline ran: the gateway was never called for this story
        assert client.get(f"/api/v1/stories/{sid}").json()["spec_version"] == 0
