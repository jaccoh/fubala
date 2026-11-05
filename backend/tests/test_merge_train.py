"""Merge train: gate-approved stories of a project batched into ONE PR.

Fake Gitea (the same handler shape as test_delivery_push's) plus real git:
the flush builds a real train/<batch> branch from origin/main, so conflicts
and ancestry judgements are real git answers, not stubs.
"""

import fnmatch
import json
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import MINIMAL_SPEC
from fastapi.testclient import TestClient

from app.main import create_app

# A hang must FAIL with a traceback, never swallow the worker to the job
# timeout (the land run hung at 83 % for 37 minutes). 120 s is well above the
# slowest of these tests — the whole pipeline plus a real git remote — and far
# below the session budget, so a wedged test dies loudly instead of silently.
pytestmark = pytest.mark.timeout(120)

# ---- git helpers (shared shape with test_delivery_push) --------------------


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
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "-C", repo, "remote", "add", "origin", str(bare)], check=True)
    subprocess.run(
        ["git", "-C", repo, "push", "-q", "origin", "HEAD:refs/heads/main"], check=True
    )
    return str(bare)


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=True
    ).stdout.strip()


class ScriptedGW:
    """spec -> ai review approve -> coder write -> code review approve.

    `content` may be a constant or a callable of the CODE-round counter, so a
    test can make two stories write genuinely different bytes to the same
    file (an add/add conflict at the flush).
    """

    def __init__(self, filename: str = "feature.py", content="VALUE = 1\n"):
        self.filename = filename
        self._content = content
        self.code_rounds = 0

    @property
    def content(self):
        if callable(self._content):
            return self._content(self.code_rounds)
        return self._content

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

        prompt = self._last_user_text(messages)
        after_tool_round = any(m.get("role") == "tool" for m in messages)
        if "=== SPEC REQUEST ===" in prompt:
            yield D(text=MINIMAL_SPEC)
        elif "=== AI REVIEW REQUEST ===" in prompt:
            yield D(text="fine\n\nVERDICT: APPROVED")
        elif "=== CODE REQUEST ===" in prompt and not after_tool_round:
            self.code_rounds += 1
            yield TC(
                id="w1",
                name="write_file",
                arguments=json.dumps({"path": self.filename, "content": self.content}),
            )
        elif "=== CODE REQUEST ===" in prompt:
            yield D(text="implemented")
        else:
            yield D(text="diff matches spec\n\nVERDICT: APPROVED")
        yield U(prompt_tokens=1, completion_tokens=1)


# ---- fake gitea ------------------------------------------------------------
#
# The delivery-push fake plus the two knobs a train needs: a per-branch head
# registry (the batch branch head the remote reports) and a per-sha status
# map keyed on the sha CI really reported on.


class _TrainHandler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, body=None):
        self.server.requests.append(
            {"method": self.command, "path": self.path, "body": body}
        )

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        body = json.loads(raw) if raw else None
        self._record(body)
        # the query string is not part of the route (update_branch posts to
        # .../update?style=merge), so judge the path alone
        path = self.path.split("?")[0].rstrip("/")
        if path.endswith("/update"):
            index = int(path.split("/")[-2])
            info = self.server.pulls.get(index)
            if info is not None and info.get("merged"):
                self._json(422, {"message": "pull request was already merged"})
                return
            if self.server.update_fails > 0:
                # Gitea refuses the branch update: main moved and the merge
                # conflicts. PR creation is NOT affected by this knob.
                self.server.update_fails -= 1
                self._json(409, {"message": "merge conflict when updating the branch"})
                return
            # a real branch update moves the PR head: the optional head change
            # the test seeded decides where to
            if self.server.update_heads.get(index):
                self.server.pull_heads[index] = self.server.update_heads[index]
                branch = self.server.pull_branches.get(index)
                if branch:
                    self.server.branches[branch] = self.server.update_heads[index]
            self.server.update_calls += 1
            self._json(200, {"message": "branch updated"})
            return
        if path.endswith("/merge"):
            index = int(path.split("/")[-2])
            info = self.server.pulls.get(index)
            if info is None:
                self._json(404, {"message": "pull request not found"})
                return
            if info.get("merged"):
                self._json(405, {"message": "The PR is already merged"})
                return
            body = body if isinstance(body, dict) else {}
            forced = bool(body.get("force_merge"))
            pinned = self.server.pull_heads.get(index)
            if pinned and pinned != body.get("head_commit_id"):
                self._json(409, {"message": "head commit does not match"})
                return
            touched = self.server.pull_files.get(index) or []
            protected = any(
                fnmatch.fnmatch(name, pat)
                for name in touched
                for pat in self.server.protected_files
            )
            if (protected and not forced) or (forced and not self.server.force_allowed):
                self._json(405, {"message": "Changed protected files"})
                return
            info["merged"] = True
            info["state"] = "closed"
            self.server.merges.append(
                {
                    "index": index,
                    "head_commit_id": body.get("head_commit_id"),
                    "force_merge": forced,
                }
            )
            self._json(200, {})
            return
        if "/actions/runs/" in path and path.endswith("/rerun"):
            self.server.rerun_calls.append(int(path.split("/")[-2]))
            self._json(201, {})
            return
        self.server.pr_creates += 1
        index = self.server.next_index
        self.server.next_index += 1
        if isinstance(body, dict) and body.get("head"):
            # the update-branch hook must be able to move the BRANCH head,
            # which is what a real Gitea does when it merges a moved main
            self.server.pull_branches[index] = str(body["head"])
        # a created PR is a real PR: later GETs (the poller) must find it
        self.server.pulls.setdefault(
            index, {"merged": False, "mergeable": True, "state": "open"}
        )
        self._json(
            201,
            {
                "html_url": f"http://gitea.local/acme/app/pulls/{index}",
                "number": index,
                "merged": False,
                "mergeable": True,
                "state": "open",
            },
        )

    def do_PATCH(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        body = json.loads(raw) if raw else None
        self._record(body)
        path = self.path.split("?")[0].rstrip("/")
        index = int(path.split("/")[-1])
        info = self.server.pulls.get(index)
        if info is None:
            self._json(404, {"message": "pull request not found"})
            return
        if info.get("merged"):
            self._json(409, {"message": "pull request already merged"})
            return
        if isinstance(body, dict) and body.get("state"):
            info["state"] = body["state"]
        self._json(200, {"number": index, **info})

    def do_DELETE(self):
        self._record(None)
        path = self.path.split("?")[0].rstrip("/")
        name = path.split("/branches/")[1]
        if self.server.branches.pop(name, None) is None:
            self._json(404, {"message": "branch not found"})
            return
        self.server.deleted_branches.append(name)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._record(None)
        path = self.path.split("?")[0].rstrip("/")
        if "/actions/" in path:
            if self.server.actions_404 and not path.endswith("/runs"):
                self._json(404, {"message": "not found"})
                return
            if path.endswith("/actions/runs"):
                self._json(200, {"workflow_runs": self.server.runs})
                return
            if path.endswith("/jobs"):
                run_id = int(path.split("/")[-2])
                self._json(200, {"jobs": self.server.jobs.get(run_id, [])})
                return
            if path.endswith("/logs"):
                job_id = int(path.split("/")[-2])
                body = self.server.job_logs.get(job_id, "").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        if "/branch_protections/" in path:
            found = self.server.protections.get(path.split("/branch_protections/")[1])
            if found is None:
                self._json(404, {"message": "branch protection not found"})
                return
            self._json(200, found)
            return
        if path.endswith("/files") and "/pulls/" in path:
            index = int(path.split("/")[-2])
            self._json(
                200,
                [{"filename": n} for n in self.server.pull_files.get(index, [])],
            )
            return
        if path.endswith("/statuses") and "/commits/" in path:
            sha = path.split("/commits/")[1].split("/")[0]
            self._json(200, (self.server.statuses.get(sha) or {}).get("statuses", []))
            return
        if "/commits/" in path:
            sha = path.split("/commits/")[1].split("/")[0]
            found = self.server.statuses.get(sha)
            self._json(200, found if found is not None else {})
            return
        if "/branches/" in path:
            name = path.split("/branches/")[1]
            head = self.server.branches.get(name)
            if head is None:
                self._json(404, {"message": "branch not found"})
                return
            self._json(200, {"name": name, "commit": {"id": head}})
            return
        if "/pulls?" in path:
            self._json(200, [])
            return
        index = int(path.split("/")[-1])
        info = self.server.pulls.get(index)
        if info is None:
            self._json(404, {"message": "pull request not found"})
            return
        self._json(
            200,
            {
                "number": index,
                **info,
                "head": {"ref": "b", "sha": self.server.pull_heads.get(index)},
            },
        )

    def log_message(self, *args):
        pass


def _fake_train_gitea():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TrainHandler)
    server.requests = []
    server.pulls = {}
    server.statuses = {}
    server.branches = {}
    server.pull_heads = {}
    server.pull_branches = {}
    server.update_heads = {}
    server.update_fails = 0
    server.update_calls = 0
    server.pr_creates = 0
    server.next_index = 100
    server.deleted_branches = []
    server.runs = []
    server.jobs = {}
    server.job_logs = {}
    server.actions_404 = False
    server.merges = []
    server.protected_files = []
    server.force_allowed = True
    server.rerun_calls = []
    server.pull_files = {}
    server.protections = {}
    server.serve_forever_thread = threading.Thread(
        target=server.serve_forever, daemon=True
    )
    server.serve_forever_thread.start()
    return server


def _assert_retired(server, batch: dict) -> None:
    """The replaced batch's PR was closed and its branch deleted."""
    wait_for(
        lambda: f"train/{batch['id']}" in server.deleted_branches,
        what=f"branch of {batch['id']} deleted",
    )
    assert any(
        r["method"] == "PATCH"
        and r["path"].endswith(f"/pulls/{batch['pr_index']}")
        and r["body"] == {"state": "closed"}
        for r in server.requests
    ), f"no PR close recorded for #{batch['pr_index']}"


def _fail_job_log(server, sha: str, log_text: str) -> None:
    """Make the fake report a failed Actions job on `sha` with this log."""
    run_id = 1000 + len(server.runs)
    job_id = 5000 + len(server.runs)
    server.runs.append({"id": run_id, "head_sha": sha})
    server.jobs[run_id] = [{"id": job_id, "conclusion": "failure"}]
    server.job_logs[job_id] = log_text


# ---- harness ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch):
    from app import runner as runner_module

    monkeypatch.setattr(runner_module, "LAND_POLL_MIN_INTERVAL", 0.0)
    monkeypatch.setenv("FUBALA_PR_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("FUBALA_PR_BACKOFF_BASE", "0")


def wait_for(predicate, timeout=60.0, what="condition"):
    """60 s, not the 30 s other files use: these tests drive the whole
    pipeline (spec → code → review → land) through a real git remote, and
    under a loaded CI node a single 30 s window can expire mid-flight."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {what}")


class Train:
    """One project with the train on, plus convenience accessors."""

    def __init__(self, client, server, project_id: str):
        self.client = client
        self.server = server
        self.project_id = project_id

    def batches(self) -> list[dict]:
        return self.client.get(f"/api/v1/projects/{self.project_id}/train").json()

    def story(self, sid: str) -> dict:
        detail = self.client.get(f"/api/v1/stories/{sid}").json()
        detail["_notes"] = [
            json.loads(e.get("data") or "{}")
            for e in detail.get("events", [])
            if e.get("kind") == "note"
        ]
        return detail

    def open_batches(self) -> list[dict]:
        return [b for b in self.batches() if b["state"] == "open"]

    def seed_batch_pr(self, status: str, description: str = "build broke") -> None:
        """Point the remote at a batch head and stamp CI on it."""
        batch = self.open_batches()[0]
        head = batch["head_sha"]
        self.server.branches[f"train/{batch['id']}"] = head
        self.server.pull_heads[batch["pr_index"]] = head
        self.server.pulls[batch["pr_index"]] = {
            "merged": False,
            "mergeable": True,
            "state": "open",
        }
        if status:
            self.server.statuses[head] = {
                "status": status,
                "statuses": [{"status": status, "description": description}],
            }


def _make_train(
    tmp_path, monkeypatch, server, *, window=1, tmax=6, train=True, gw=None,
    hold_forming=True,
):
    repo = init_repo(tmp_path)
    bare = init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "train.db"))
    # Deterministic formation (the review's fix): while this flag is set a
    # forming batch never departs, so the test decides when the batch has all
    # its members and releases the flag itself — no wall clock anywhere.
    from app import train as train_module

    monkeypatch.setattr(train_module, "TRAIN_HOLD_FORMING", hold_forming)
    client = TestClient(
        create_app(gateway=gw or ScriptedGW())
    )
    client.__enter__()
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
            "merge_train": train,
            "train_window_s": window,
            "train_max": tmax,
        },
    ).json()
    return client, project, repo, bare


def _run_story(client, project_id: str) -> str:
    story = client.post(
        "/api/v1/stories", json={"title": "feature", "project_id": project_id}
    ).json()
    sid = story["id"]
    client.post(f"/api/v1/stories/{sid}/queue")
    approved = None
    # two gates: the spec gate's approve starts execution, the code gate's
    # approve lands the branch. Loop until an approve actually lands.
    for _ in range(4):
        current = None
        deadline = time.time() + 60
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{sid}").json()
            if current["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert current is not None and current["status"] == "human_review", (
            current and current.get("error")
        )
        approved = client.post(
            f"/api/v1/stories/{sid}/approve", json={"by": "t"}
        ).json()
        if approved["status"] == "landing":
            return sid
    pytest.fail(f"never landed: {approved}")


def _merge_main(repo: str, bare: str) -> None:
    """Simulate the batch PR merging: fast-forward main to the batch branch."""
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "merge", "--ff-only", "origin/train/HEAD")


def _run_stories_together(client, project_id: str, count: int) -> list[str]:
    """Run N stories CONCURRENTLY through both gates.

    Sequential _run_story calls race the train window under load: story 2
    only starts after story 1 has landed in a forming batch, and with a
    short window the batch departs before story 2 joins, so the "one batch"
    the tests assume never forms. Concurrent stories all sit at the code
    gate together, and the holds below keep the window open until every one
    of them has joined.
    """
    sids: list[str] = []
    for _ in range(count):
        story = client.post(
            "/api/v1/stories", json={"title": "feature", "project_id": project_id}
        ).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        sids.append(story["id"])
    landed: dict[str, dict] = {}
    deadline = time.time() + 90.0
    # spec gate -> execution -> code gate, all N at once. A story sits at
    # human_review TWICE (spec gate, then code gate): the first approve only
    # starts execution, so a story counts as done when an approve answers
    # "landing" — never before.
    while len(landed) < count and time.time() < deadline:
        for sid in sids:
            if sid in landed:
                continue
            current = client.get(f"/api/v1/stories/{sid}").json()
            if current["status"] == "failed":
                pytest.fail(f"story {sid} failed: {current.get('error')}")
            if current["status"] != "human_review":
                continue
            approved = client.post(
                f"/api/v1/stories/{sid}/approve", json={"by": "t"}
            ).json()
            assert approved["status"] in ("landing", "executing"), approved
            if approved["status"] == "landing":
                landed[sid] = approved
        time.sleep(0.05)
    assert len(landed) == count, f"only {len(landed)}/{count} stories landed"
    return sids


def _polls_on(server, sha: str) -> int:
    """How many CI-status reads the poller made on `sha` (observed evidence
    that a poll actually judged that head, not a sleep)."""
    return sum(
        1
        for r in server.requests
        if r["method"] == "GET" and f"/commits/{sha}" in r["path"]
    )


def _story_land_state(path: Path, sid: str) -> str | None:
    """The member's land_state straight from SQLite (the API does not expose
    it), used to observe the dwell clock without sleeping."""
    return _read_db(path, "SELECT land_state FROM stories WHERE id=?", (sid,))[0][0]


def _count_train_ticks(monkeypatch) -> "list[int]":
    """Observe scheduler ticks directly: runner calls train_module.train_tick
    on every tick, so a counting wrapper (module attribute, looked up at call
    time) is the honest "a tick ran" signal — a forming batch makes no HTTP
    requests, so there is nothing on the wire to count."""
    from app import train as train_module

    counter = [0]
    real = train_module.train_tick

    def counting(db):
        counter[0] += 1
        return real(db)

    monkeypatch.setattr(train_module, "train_tick", counting)
    return counter


def _depart(t, joined: int, *, what: str, open_members: int | None = None) -> dict:
    """Release the formation hold and wait for the departure.

    Formation never touches the wall clock: while TRAIN_HOLD_FORMING is set
    the batch cannot depart at all, so the test first observes exactly one
    forming batch holding `joined` members (the shape it needs), and only
    then lowers the flag — membership at the departure is what was observed,
    never what a timer happened to allow. `open_members` covers the conflict
    case, where a member is dropped at the flush and the batch opens smaller
    than it formed.
    """
    from app import train as train_module

    forming = [b for b in t.batches() if b["state"] == "forming"]
    assert len(forming) == 1, f"expected exactly one forming batch: {t.batches()}"
    assert len(forming[0]["members"]) == joined, forming[0]
    expect = open_members if open_members is not None else joined
    train_module.TRAIN_HOLD_FORMING = False

    def departed() -> list[dict]:
        return [
            b
            for b in t.batches()
            if b["state"] == "open" and len(b["members"]) == expect
        ]

    wait_for(lambda: len(departed()) == 1, what=what)
    return departed()[0]


def _wait_one_batch(t, want: int, *, what: str) -> dict:
    """Wait until exactly ONE open batch carries `want` members.

    A flush that ran early (under load, before every member joined) would
    split the members across batches, so the wait is on the SHAPE the tests
    need, not merely on "a batch exists".
    """

    def one_full_batch():
        open_batches = [
            b for b in t.batches() if b["state"] == "open" and len(b["members"]) == want
        ]
        return len(open_batches) == 1 and not [
            b for b in t.batches() if b["state"] == "forming"
        ]

    wait_for(one_full_batch, what=what)
    return next(
        b for b in t.batches() if b["state"] == "open" and len(b["members"]) == want
    )


def _write_db(db_path: Path, sql: str, params: tuple) -> None:
    """Stage a crash shape directly in SQLite (the runner's engine is live
    in another thread, so retry the write while it holds the lock)."""
    import sqlite3

    deadline = time.time() + 10
    while True:
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(sql, params)
            conn.commit()
            return
        except sqlite3.OperationalError:
            if time.time() > deadline:
                raise
            time.sleep(0.05)
        finally:
            conn.close()


def _read_db(db_path: Path, sql: str, params: tuple):
    import sqlite3

    conn = sqlite3.connect(db_path, timeout=5)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _batch_state(db_path: Path, batch_id: str) -> str | None:
    rows = _read_db(
        db_path, "SELECT state FROM train_batches WHERE id=?", (batch_id,)
    )
    return rows[0][0] if rows else None


def _stage_batch_state(db_path: Path, batch_id: str, state: str, ticks) -> None:
    """Put a batch in `state` and hold it there across a tick boundary.

    A tick that read the batch before the write can still be in flight; if
    its own commit flips the state back, rewrite it, so the staged shape
    (the crash window this test is about) is what the next sweeps see.
    `ticks` is _count_train_ticks' counter: the hold lasts until a WHOLE
    tick has run with the staged state in place (observed), not for a fixed
    sleep — the tick could otherwise still be mid-judgement when the test
    moves on, and under load that race leaked the landing to the ordinary
    OPEN path.
    """
    _write_db(
        db_path, "UPDATE train_batches SET state=? WHERE id=?", (state, batch_id)
    )
    deadline = time.time() + 30.0
    while time.time() < deadline:
        if _batch_state(db_path, batch_id) != state:
            _write_db(
                db_path,
                "UPDATE train_batches SET state=? WHERE id=?",
                (state, batch_id),
            )
        # a full tick AFTER the last rewrite is the evidence that no
        # in-flight judgement can flip the state back anymore
        seen = ticks[0]
        if _ticked_past(ticks, seen, db_path, batch_id, state):
            return
        time.sleep(0.05)
    assert _batch_state(db_path, batch_id) == state


def _ticked_past(counter, seen: int, db_path: Path, batch_id: str, state: str):
    """Wait (bounded, inside the stage loop) for one more counted tick that
    left the staged state alone."""
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if counter[0] > seen and _batch_state(db_path, batch_id) == state:
            return True
        if _batch_state(db_path, batch_id) != state:
            return False  # flipped back: rewrite and count again
        time.sleep(0.05)
    return False


# ---- project config --------------------------------------------------------


def test_train_cannot_be_enabled_without_push_and_pr_wiring(tmp_path, monkeypatch):
    server = _fake_train_gitea()
    client = None
    try:
        client, project, _repo, _bare = _make_train(
            tmp_path, monkeypatch, server, train=False
        )
        # strip the wiring and drop back to merge delivery: the train is off,
        # so the project is fine — this is a legacy-shaped project
        client.patch(
            f"/api/v1/projects/{project['id']}",
            json={
                "delivery": "merge",
                "pr_api_base": None,
                "pr_repo": None,
                "pr_base_branch": None,
                "pr_token_file": None,
            },
        )
        # train on while delivery is merge: refused
        res = client.patch(
            f"/api/v1/projects/{project['id']}", json={"merge_train": True}
        )
        assert res.status_code == 422
        assert "merge train requires" in res.json()["detail"]
        # push delivery but the pr_* wiring missing: still refused
        client.patch(f"/api/v1/projects/{project['id']}", json={"delivery": "push"})
        assert client.patch(
            f"/api/v1/projects/{project['id']}", json={"merge_train": True}
        ).status_code == 422
        # wiring restored: allowed, and visible in the project payload
        client.patch(
            f"/api/v1/projects/{project['id']}",
            json={
                "pr_api_base": "http://127.0.0.1:1",
                "pr_repo": "acme/app",
                "pr_base_branch": "main",
                "pr_token_file": "/tmp/t.txt",
            },
        )
        ok = client.patch(
            f"/api/v1/projects/{project['id']}", json={"merge_train": True}
        ).json()
        assert ok["merge_train"] is True
        assert ok["train_window_s"] == 1
        assert ok["train_max"] == 6
        # ... and stripping the wiring again is refused
        res = client.patch(
            f"/api/v1/projects/{project['id']}", json={"pr_repo": None}
        )
        assert res.status_code == 422
    finally:
        _stop_server(server)
        if client:
            _close_clients(client)


def _close_clients(client):
    try:
        client.__exit__(None, None, None)
    except Exception:  # noqa: BLE001
        pass


def _stop_server(server) -> None:
    """Bounded teardown: never hangs a failing test session.

    shutdown() blocks forever when serve_forever is not running (the test
    failed before the server started), so guard it on the serve thread; the
    join is bounded so a wedged handler thread cannot hold the worker.
    """
    thread = getattr(server, "serve_forever_thread", None)
    if thread is not None and thread.is_alive():
        server.shutdown()
        thread.join(timeout=5.0)
    try:
        server.server_close()
    except Exception:  # noqa: BLE001
        pass
    # the per-request handler threads: bounded, best-effort. socketserver
    # parks a non-iterable _NoThreads sentinel on the attribute when no
    # threads are being tracked (daemon_threads), so guard the iteration.
    threads = getattr(server, "_threads", None)
    if isinstance(threads, list):
        for thread in list(threads):
            try:
                thread.join(timeout=1.0)
            except Exception:  # noqa: BLE001
                pass


# ---- happy path ------------------------------------------------------------


def test_three_green_stories_land_through_one_batch_pr(tmp_path, monkeypatch):
    server = _fake_train_gitea()
    client = None
    try:
        client, project, repo, bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        t = Train(client, server, project["id"])
        sids = _run_stories_together(client, project["id"], 3)
        batch = _depart(t, 3, what="the batch to open")
        assert server.pr_creates == 1, server.requests
        assert sorted(m["id"] for m in batch["members"]) == sorted(sids)
        assert batch["pr_url"].endswith(f"/pulls/{batch['pr_index']}")

        # the branch really carries all three stories
        t.seed_batch_pr("success", "all good")
        head = batch["head_sha"]
        assert _git(bare, "rev-parse", "--verify", f"refs/heads/train/{batch['id']}")

        # CI green + PR merged -> every member done, note names the batch PR
        server.pulls[batch["pr_index"]] = {
            "merged": True,
            "mergeable": True,
            "state": "open",
        }
        # the real merge: main moves to the batch branch
        _git(repo, "fetch", "-q", "origin")
        _git(
            repo, "merge", "--ff-only", f"origin/train/{batch['id']}"
        )
        _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(repo, "reset", "-q", "--hard", "origin/main")

        for sid in sids:
            wait_for(
                lambda sid=sid: client.get(f"/api/v1/stories/{sid}").json()["status"]
                == "done",
                what=f"{sid} done",
            )
        for sid in sids:
            story = t.story(sid)
            assert story["error"] is None
            merged_notes = [
                n for n in story["_notes"] if n.get("merged") and n.get("merge_train")
            ]
            assert merged_notes, story["_notes"]
            assert batch["pr_url"] in (merged_notes[-1].get("reason") or "")
        assert head
        # the report shows the merged batch
        assert any(b["state"] == "merged" for b in t.batches())
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


def test_train_report_endpoint_lists_forming_members(tmp_path, monkeypatch):
    server = _fake_train_gitea()
    client = None
    try:
        client, project, _repo, _bare = _make_train(
            tmp_path, monkeypatch, server, window=60
        )
        t = Train(client, server, project["id"])
        sid = _run_story(client, project["id"])
        wait_for(lambda: len(t.batches()) == 1, what="a forming batch")
        batch = t.batches()[0]
        assert batch["state"] == "forming"
        assert batch["pr_url"] is None
        assert [m["id"] for m in batch["members"]] == [sid]
        assert batch["members"][0]["title"] == "feature"
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


# ---- red handling ----------------------------------------------------------


def test_red_once_then_green_lands_everyone_with_the_re_run_note(
    tmp_path, monkeypatch
):
    server = _fake_train_gitea()
    client = None
    try:
        client, project, repo, bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        t = Train(client, server, project["id"])
        sids = _run_stories_together(client, project["id"], 2)
        batch = _depart(t, 2, what="the batch to open")

        # first red on the judged head: exactly ONE re-run
        t.seed_batch_pr("failure", "flaky worker")
        wait_for(lambda: server.update_calls == 1, what="the re-run")
        # the re-run moved the head: CI on the OLD head must not count
        new_head = f"{batch['head_sha']}-updated"
        server.branches[f"train/{batch['id']}"] = new_head
        server.statuses[new_head] = {
            "status": "success",
            "statuses": [{"status": "success", "description": "green now"}],
        }
        for sid in sids:
            wait_for(
                lambda sid=sid: any(
                    n.get("re_run") for n in t.story(sid)["_notes"]
                ),
                what=f"the re-run note on {sid}",
            )
        # green on the new head -> everyone lands
        server.pulls[batch["pr_index"]] = {
            "merged": True,
            "mergeable": True,
            "state": "open",
        }
        _git(repo, "fetch", "-q", "origin")
        _git(repo, "merge", "--ff-only", f"origin/train/{batch['id']}")
        _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(repo, "reset", "-q", "--hard", "origin/main")
        for sid in sids:
            wait_for(
                lambda sid=sid: client.get(f"/api/v1/stories/{sid}").json()["status"]
                == "done",
                what=f"{sid} done",
            )
        # no child batches: the batch was never bisected
        assert all(not b["parent_batch_id"] for b in t.batches())
        assert server.update_calls == 1
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


def test_red_twice_bisects_into_two_children(tmp_path, monkeypatch):
    server = _fake_train_gitea()
    client = None
    try:
        client, project, _repo, _bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        t = Train(client, server, project["id"])
        sids = _run_stories_together(client, project["id"], 4)
        parent = _depart(t, 4, what="the batch to open")

        # red, re-run (still red on the re-run head), then red again = bisect
        t.seed_batch_pr("failure", "bad test")
        wait_for(lambda: server.update_calls == 1, what="the first re-run")
        # the re-run keeps the same head, still red -> second confirmed red
        wait_for(
            lambda: len([b for b in t.batches() if b["parent_batch_id"]]) == 2,
            what="the two child batches",
        )
        children = [b for b in t.batches() if b["parent_batch_id"]]
        sizes = sorted(len(c["members"]) for c in children)
        assert sizes == [2, 2], sizes
        assert parent["id"] in {c["parent_batch_id"] for c in children}
        # the parent is terminal
        assert next(
            b for b in t.batches() if b["id"] == parent["id"]
        )["state"] == "red"
        _assert_retired(server, parent)
        # every member was placed in exactly one child
        placed = [m["id"] for c in children for m in c["members"]]
        assert sorted(placed) == sorted(sids)
        # no 1-member "1+0" split ever happened
        assert all(len(c["members"]) >= 1 for c in children)
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


def test_single_member_batch_red_twice_bounces_to_the_coder(tmp_path, monkeypatch):
    server = _fake_train_gitea()
    client = None
    try:
        client, project, repo, _bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        t = Train(client, server, project["id"])
        sid = _run_story(client, project["id"])
        batch = _depart(t, 1, what="the batch to open")
        assert len(batch["members"]) == 1

        t.seed_batch_pr("failure", "unit test xyz failed")
        wait_for(lambda: server.update_calls == 1, what="the re-run")
        wait_for(
            lambda: client.get(f"/api/v1/stories/{sid}").json()["status"]
            == "human_review",
            what="the bounce to the coder",
        )
        story = client.get(f"/api/v1/stories/{sid}").json()
        assert story["error"] is not None
        assert "unit test xyz failed" in story["error"]
        assert story["error"].startswith("land failed:")
        # no child batch was created for a 1-member batch
        assert not [b for b in t.batches() if b["parent_batch_id"]]
        # the batch is terminal-red
        assert all(
            b["state"] != "open" for b in t.batches()
        ), t.batches()
        # The worktree is KEPT on a bounce (spec step 2): the coder's rework
        # starts from the story's own change, not from an empty checkout.
        # workspaces.discard deletes the worktree AND the local branch, and
        # create() rebuilds from main — so a worktree discarded here would
        # hand the coder none of its own code.
        from app import workspaces as workspaces_module

        wt = Path(repo) / workspaces_module.WORKTREE_DIR / sid
        assert wt.exists(), "the bounced member's worktree must survive the bounce"
        feature = wt / "feature.py"
        assert feature.exists(), f"the story's change is gone: {feature}"
        assert "VALUE" in feature.read_text()
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


def test_merged_batch_resumes_members_left_landing_by_a_crash(
    tmp_path, monkeypatch
):
    """Crash window in _land_batch: the batch row is committed MERGED before
    the member loop runs. A crash in between parks members in LANDING with
    train_batch_id set — the poller skips them and the sweep only visited
    OPEN batches. The sweep must resume a MERGED batch's member loop."""
    server = _fake_train_gitea()
    client = None
    try:
        client, project, repo, bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        t = Train(client, server, project["id"])
        ticks = _count_train_ticks(monkeypatch)
        sids = _run_stories_together(client, project["id"], 3)
        batch = _depart(t, 3, what="the batch to open")
        # green CI, PR still unmerged, main not advanced: an OPEN judgement
        # of this batch is a no-op, which is what lets us stage the crash
        # underneath a live sweep.
        t.seed_batch_pr("success", "ok")

        # Stage the crash FIRST (state='merged') while the fake PR is still
        # unmerged and main has not moved, so no member can possibly be
        # judged yet: nothing but the MERGED-resume path can land them. The
        # state is HELD until a whole tick has passed with the staged state
        # in place — an in-flight tick that read the batch OPEN commits right
        # after its Gitea reads and could otherwise stamp it straight back to
        # open, which would hand the landing to the ordinary OPEN path
        # (exactly what this test must not exercise).
        _stage_batch_state(tmp_path / "train.db", batch["id"], "merged", ticks)
        assert _batch_state(tmp_path / "train.db", batch["id"]) == "merged"

        # ... and only NOW the merge lands on the remote
        server.pulls[batch["pr_index"]] = {
            "merged": True,
            "mergeable": True,
            "state": "open",
        }
        _git(repo, "fetch", "-q", "origin")
        _git(repo, "merge", "--ff-only", f"origin/train/{batch['id']}")
        _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(repo, "reset", "-q", "--hard", "origin/main")

        for sid in sids:
            wait_for(
                lambda sid=sid: client.get(f"/api/v1/stories/{sid}").json()[
                    "status"
                ]
                == "done",
                what=f"{sid} done after the resume",
            )
        merged = next(b for b in t.batches() if b["id"] == batch["id"])
        assert merged["state"] == "merged"
        for sid in sids:
            notes = [
                n
                for n in t.story(sid)["_notes"]
                if n.get("merged") and n.get("merge_train")
            ]
            assert notes, f"no merged note on {sid}"
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


def test_merged_batch_missing_from_main_dwells_before_the_bounce(
    tmp_path, monkeypatch
):
    """Gitea reports the batch PR merged while main does not yet carry the
    member branches. One reading is a propagation race — the member stays
    LANDING and is never bounced. A missing that persists past
    LAND_MISSING_MIN_DWELL bounces the member to the gate."""
    from app import train as train_module

    server = _fake_train_gitea()
    client = None
    try:
        client, project, repo, _bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        t = Train(client, server, project["id"])
        sid = _run_story(client, project["id"])
        batch = _depart(t, 1, what="the batch to open")
        t.seed_batch_pr("success", "ok")
        # the PR is merged but main is NOT advanced: the member branch is
        # not an ancestor of main yet — the propagation race window
        server.pulls[batch["pr_index"]] = {
            "merged": True,
            "mergeable": True,
            "state": "open",
        }
        monkeypatch.setattr(train_module, "LAND_MISSING_MIN_DWELL", 3600.0)
        # a fresh reading is a race, not a verdict: no bounce. Observed, not
        # slept: the poller must have judged the batch AND started the dwell
        # clock on the member (land_state carries the _LAND_MISSING stamp)
        # and the member still sits in landing with no error.
        wait_for(
            lambda: next(b for b in t.batches() if b["id"] == batch["id"])[
                "state"
            ]
            == "merged",
            what="the merged stamp (the poller judged the batch)",
        )
        wait_for(
            lambda: train_module._LAND_MISSING in str(
                _story_land_state(tmp_path / "train.db", sid)
            ),
            what="the dwell clock to start on the member",
        )
        story = client.get(f"/api/v1/stories/{sid}").json()
        assert story["status"] == "landing", story
        assert story["error"] is None, story
        # the missing persists past the dwell: the bounce fires
        monkeypatch.setattr(train_module, "LAND_MISSING_MIN_DWELL", 0.0)
        wait_for(
            lambda: client.get(f"/api/v1/stories/{sid}").json()["status"]
            == "human_review",
            what="the persisting-missing bounce",
        )
        bounced = client.get(f"/api/v1/stories/{sid}").json()
        assert bounced["error"] is not None
        assert "not in main after the batch merge" in bounced["error"]
        assert bounced["error"].startswith("land failed:")
        # the batch itself is settled: merged, no children
        assert any(
            b["state"] == "merged" and b["id"] == batch["id"]
            for b in t.batches()
        )
        assert not [b for b in t.batches() if b["parent_batch_id"]]
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


def test_merged_batch_without_the_members_own_commit_bounces(
    tmp_path, monkeypatch
):
    """The empty-land done gate: a member whose pushed_sha is an ancestor of
    the batch's flush-time base (the branch added nothing of its own) is
    never DONE — the member-ref check alone would pass, because even an
    empty branch is an ancestor of main. It bounces with the absent-commit
    reason and keeps its worktree."""
    from app import train as train_module
    from app import workspaces as workspaces_module

    server = _fake_train_gitea()
    client = None
    try:
        # dwell 0: the bounce must follow the first dwell-stamped reading on
        # the very next tick, no wall clock
        monkeypatch.setattr(train_module, "LAND_MISSING_MIN_DWELL", 0.0)
        client, project, repo, _bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        t = Train(client, server, project["id"])
        sid = _run_story(client, project["id"])
        batch = _depart(t, 1, what="the batch to open")
        t.seed_batch_pr("success", "ok")

        # the incident shape, replayed on the row: pushed_sha points at the
        # batch base — the branch carried no story work of its own
        base_sha = _read_db(
            tmp_path / "train.db",
            "SELECT base_sha FROM train_batches WHERE id=?",
            (batch["id"],),
        )[0][0]
        assert base_sha, "the flush must pin the batch base"
        _write_db(
            tmp_path / "train.db",
            "UPDATE stories SET pushed_sha=? WHERE id=?",
            (base_sha, sid),
        )

        # the batch PR merges for real: main now carries the member branch
        # (the member-ref check alone would read this member as landed)
        server.pulls[batch["pr_index"]] = {
            "merged": True,
            "mergeable": True,
            "state": "open",
        }
        _git(repo, "fetch", "-q", "origin")
        _git(repo, "merge", "--ff-only", f"origin/train/{batch['id']}")
        _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(repo, "reset", "-q", "--hard", "origin/main")

        wait_for(
            lambda: client.get(f"/api/v1/stories/{sid}").json()["status"]
            == "human_review",
            what="the absent-commit bounce",
        )
        bounced = client.get(f"/api/v1/stories/{sid}").json()
        assert bounced["error"] is not None
        assert "story commit absent from merged batch" in bounced["error"]
        assert bounced["error"].startswith("land failed:")
        assert "not in main" not in bounced["error"]
        # never DONE, and the story's own work survives the bounce
        wt = Path(repo) / workspaces_module.WORKTREE_DIR / sid
        assert wt.exists(), "the bounced member's worktree must survive"
        assert (wt / "feature.py").exists()
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


def test_busy_pipeline_departure_is_bounded_by_train_max_hold_s(
    tmp_path, monkeypatch
):
    """The flush hold is bounded by ONE constant: a batch departs at most
    TRAIN_MAX_HOLD_S after its window expired, busy pipeline or not."""
    from app import train as train_module

    window = 60
    hold = train_module.TRAIN_MAX_HOLD_S
    server = _fake_train_gitea()
    client = None
    try:
        client, project, repo, _bare = _make_train(
            tmp_path, monkeypatch, server, window=window, tmax=6, hold_forming=False
        )
        ticks = _count_train_ticks(monkeypatch)
        t = Train(client, server, project["id"])
        sid = _run_story(client, project["id"])
        wait_for(
            lambda: any(b["state"] == "forming" for b in t.batches()),
            what="the forming batch",
        )
        batch = next(b for b in t.batches() if b["state"] == "forming")

        # Keep the pipeline "busy" with a story parked in the busy statuses,
        # so only the TRAIN_MAX_HOLD_S bound can release the batch.
        filler = client.post(
            "/api/v1/stories",
            json={"title": "parked", "project_id": project["id"]},
        ).json()
        _write_db(
            tmp_path / "train.db",
            "UPDATE stories SET status='in_progress' WHERE id=?",
            (filler["id"],),
        )
        # Ten seconds BEFORE the bound: the busy pipeline still holds it.
        # Observed, not slept: several scheduler ticks must have run on the
        # still-forming batch (the train report keeps returning it) without
        # flushing it.
        _write_db(
            tmp_path / "train.db",
            "UPDATE train_batches SET created_at=? WHERE id=?",
            (
                (
                    datetime.now(UTC) - timedelta(seconds=window + hold - 10)
                ).isoformat(),
                batch["id"],
            ),
        )
        wait_for(
            lambda: ticks[0] >= 2,
            what="scheduler ticks that kept the batch forming",
        )
        assert not t.open_batches(), "the batch departed before its bound"

        # at window + TRAIN_MAX_HOLD_S it departs, busy or not
        _write_db(
            tmp_path / "train.db",
            "UPDATE train_batches SET created_at=? WHERE id=?",
            (
                (datetime.now(UTC) - timedelta(seconds=window + hold)).isoformat(),
                batch["id"],
            ),
        )
        wait_for(
            lambda: t.open_batches(),
            what="the departure at window + TRAIN_MAX_HOLD_S",
        )
        assert [m["id"] for m in t.open_batches()[0]["members"]] == [sid]
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


def test_bisect_isolates_the_one_red_story_of_four(tmp_path, monkeypatch):
    server = _fake_train_gitea()
    client = None
    try:
        client, project, repo, bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        t = Train(client, server, project["id"])
        sids = _run_stories_together(client, project["id"], 4)
        parent = _depart(t, 4, what="the batch to open")

        # level 0: the whole batch is red
        t.seed_batch_pr("failure", "level0 red")
        wait_for(lambda: server.update_calls == 1, what="the level0 re-run")
        wait_for(
            lambda: len([b for b in t.batches() if b["parent_batch_id"]]) == 2,
            what="the level0 split",
        )

        # the children take the project's single CI slot ONE AT A TIME: the
        # second stays forming until the first is merged or red
        def _open_of(parent_id: str, skip: tuple = ()) -> list[dict]:
            return [
                b
                for b in t.batches()
                if b["parent_batch_id"] == parent_id
                and b["state"] == "open"
                and b["id"] not in skip
            ]

        def _seed(b: dict, status: str, description: str, merged: bool) -> None:
            server.branches[f"train/{b['id']}"] = b["head_sha"]
            server.pull_heads[b["pr_index"]] = b["head_sha"]
            server.pulls[b["pr_index"]] = {
                "merged": merged,
                "mergeable": True,
                "state": "open",
            }
            server.statuses[b["head_sha"]] = {
                "status": status,
                "statuses": [{"status": status, "description": description}],
            }

        def _ff_main(b: dict) -> None:
            _git(repo, "fetch", "-q", "origin")
            _git(repo, "merge", "--ff-only", f"origin/train/{b['id']}")
            _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
            _git(repo, "reset", "-q", "--hard", "origin/main")

        wait_for(lambda: len(_open_of(parent["id"])) == 1, what="first child open")
        green = _open_of(parent["id"])[0]
        assert not [
            b
            for b in t.batches()
            if b["parent_batch_id"] == parent["id"] and b["state"] == "open"
            and b["id"] != green["id"]
        ]
        # the green half merges and lands its two members
        _seed(green, "success", "ok", merged=True)
        _ff_main(green)
        wait_for(
            lambda: all(
                client.get(f"/api/v1/stories/{m['id']}").json()["status"] == "done"
                for m in green["members"]
            ),
            what="the green half to land",
        )
        # only now does the second child take the slot
        wait_for(
            lambda: len(_open_of(parent["id"], (green["id"],))) == 1,
            what="second child open",
        )
        red = _open_of(parent["id"], (green["id"],))[0]
        _seed(red, "failure", "level1 red", merged=False)
        # the red half: one re-run, then still red -> two 1-member children
        wait_for(
            lambda: server.update_calls == 2, what="the level1 re-run"
        )
        wait_for(
            lambda: len(
                [b for b in t.batches() if b["parent_batch_id"] == red["id"]]
            )
            == 2,
            what="the level1 split",
        )
        leaves = [b for b in t.batches() if b["parent_batch_id"] == red["id"]]
        assert sorted(len(b["members"]) for b in leaves) == [1, 1], leaves
        # Exactly ONE of the four stories is broken. The green half already
        # landed its two; of the two leaves left, the clean one merges first
        # and lands, then the other takes the slot and is red.
        wait_for(lambda: len(_open_of(red["id"])) == 1, what="first leaf open")
        good_leaf = _open_of(red["id"])[0]
        _seed(good_leaf, "success", "ok", merged=True)
        _ff_main(good_leaf)
        wait_for(
            lambda: len(_open_of(red["id"], (good_leaf["id"],))) == 1,
            what="second leaf open",
        )
        bad_leaf = _open_of(red["id"], (good_leaf["id"],))[0]
        _seed(bad_leaf, "failure", "leaf red", merged=False)

        # the red leaf burns its one re-run, stays red, and bounces its
        # single member to the coder with the CI failure as the error
        wait_for(
            lambda: server.update_calls == 3, what="the leaf re-run"
        )
        bounced = bad_leaf["members"][0]["id"]
        wait_for(
            lambda: client.get(f"/api/v1/stories/{bounced}").json()["status"]
            == "human_review",
            what="the red leaf to bounce",
            timeout=40.0,
        )
        story = client.get(f"/api/v1/stories/{bounced}").json()
        assert story["error"] is not None
        assert "leaf red" in story["error"]
        assert story["error"].startswith("land failed:")
        # no deeper split: a 1-member batch is never split
        assert not [
            b for b in t.batches() if b["parent_batch_id"] == bad_leaf["id"]
        ]
        # the other three landed done
        for sid in sids:
            if sid == bounced:
                continue
            wait_for(
                lambda sid=sid: client.get(f"/api/v1/stories/{sid}").json()["status"]
                == "done",
                what=f"{sid} done",
            )
        assert sum(
            1
            for sid in sids
            if client.get(f"/api/v1/stories/{sid}").json()["status"] == "done"
        ) == 3
        # and no batch is left judging anything
        wait_for(
            lambda: not t.open_batches(), what="no batch left open"
        )
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


def test_update_branch_moving_the_head_is_judged_and_not_stranded(
    tmp_path, monkeypatch
):
    """The update-branch hook itself moves the head (it merged a moved main):
    the poller must judge the NEW head, so a red on it bisects and a green
    lands — the batch is never stranded on the old head."""
    server = _fake_train_gitea()
    client = None
    try:
        client, project, repo, bare = _make_train(
            tmp_path, monkeypatch, server, window=60, tmax=2
        )
        t = Train(client, server, project["id"])
        sids = _run_stories_together(client, project["id"], 2)
        batch = _depart(t, 2, what="the batch to open")
        assert sorted(m["id"] for m in batch["members"]) == sorted(sids)
        t.seed_batch_pr("failure", "red on the flushed head")

        # the update-branch hook moves the PR head itself (like a real Gitea
        # merging a moved main): CI then reports on THAT sha only
        moved = f"{batch['head_sha']}-after-update"
        server.update_heads[batch["pr_index"]] = moved
        wait_for(lambda: server.update_calls == 1, what="the re-run")
        wait_for(
            lambda: t.open_batches()
            and t.open_batches()[0]["head_sha"] == moved,
            what="the stored head to become the moved head",
        )
        for sid in sids:
            wait_for(
                lambda sid=sid: any(
                    n.get("re_run") for n in t.story(sid)["_notes"]
                ),
                what=f"the re-run note on {sid}",
            )

        # a green on the MOVED head lands the whole batch: not stranded
        server.statuses[moved] = {
            "status": "success",
            "statuses": [{"status": "success", "description": "green now"}],
        }
        server.pulls[batch["pr_index"]] = {
            "merged": True,
            "mergeable": True,
            "state": "open",
        }
        _git(repo, "fetch", "-q", "origin")
        _git(repo, "merge", "--ff-only", f"origin/train/{batch['id']}")
        _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(repo, "reset", "-q", "--hard", "origin/main")
        for sid in sids:
            wait_for(
                lambda sid=sid: client.get(f"/api/v1/stories/{sid}").json()["status"]
                == "done",
                what=f"{sid} done",
            )
        # the batch landed whole: no bisect ever happened
        assert all(not b["parent_batch_id"] for b in t.batches())
        assert server.update_calls == 1
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


def test_stale_status_on_a_replaced_head_is_never_a_verdict(tmp_path, monkeypatch):
    """The re-run moves the head: a red left on the OLD head must not bisect
    the batch — only statuses on the STORED head count."""
    server = _fake_train_gitea()
    client = None
    try:
        client, project, _repo, _bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        t = Train(client, server, project["id"])
        _run_stories_together(client, project["id"], 2)
        batch = _depart(t, 2, what="the batch to open")
        t.seed_batch_pr("failure", "old head red")
        wait_for(lambda: server.update_calls == 1, what="the re-run")
        # the re-run moved the head; the new head has NO status yet
        new_head = "deadbeef"
        server.branches[f"train/{batch['id']}"] = new_head
        # give the poller real polls on the replaced head — but bounded and
        # observed, not a blind sleep: the poller must have READ the new head
        # (head_sha stored) and still done nothing with the stale red.
        wait_for(
            lambda: next(
                b for b in t.batches() if b["id"] == batch["id"]
            )["head_sha"]
            == new_head,
            what="the poller to read the replaced head",
        )
        wait_for(
            lambda: _polls_on(server, new_head) >= 2,
            what="a second poll on the replaced head",
        )
        assert not [b for b in t.batches() if b["parent_batch_id"]], t.batches()
        assert t.open_batches(), "the batch must still be open and waiting"
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


def test_update_branch_failure_reflushes_from_main(tmp_path, monkeypatch):
    """update_branch 409s (main moved, conflict): the members re-flush from
    current main in a new batch carrying the red count over."""
    server = _fake_train_gitea()
    client = None
    try:
        client, project, _repo, _bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        t = Train(client, server, project["id"])
        sids = _run_stories_together(client, project["id"], 2)
        original = _depart(t, 2, what="the batch to open")
        t.seed_batch_pr("failure", "red")
        # the re-run cannot be done server-side: Gitea refuses the update
        server.update_fails = 5
        wait_for(
            lambda: any(
                b["parent_batch_id"] is None
                and b["id"] != original["id"]
                and b["state"] == "open"
                for b in t.batches()
            ),
            what="the re-flushed batch",
        )
        reflushed = next(
            b
            for b in t.batches()
            if b["id"] != original["id"] and b["state"] == "open"
        )
        assert sorted(m["id"] for m in reflushed["members"]) == sorted(sids)
        assert reflushed["red_runs"] == 1, reflushed
        _assert_retired(server, original)
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


# ---- infra timeouts and the single CI slot ---------------------------------


@pytest.mark.parametrize("marker", ["land pod", "test pod"])
def test_ci_pod_timeout_retries_the_same_batch_later_without_bisecting(
    tmp_path, monkeypatch, caplog, marker
):
    import logging
    from datetime import timedelta

    from app import train as train_module

    server = _fake_train_gitea()
    client = None
    try:
        client, project, _repo, _bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        offset = [0.0]
        real_now = train_module.train_now
        monkeypatch.setattr(
            train_module,
            "train_now",
            lambda: real_now() + timedelta(seconds=offset[0]),
        )
        ticks = _count_train_ticks(monkeypatch)
        t = Train(client, server, project["id"])
        sids = _run_stories_together(client, project["id"], 2)
        original = _depart(t, 2, what="the batch to open")
        caplog.set_level(logging.WARNING)
        _fail_job_log(
            server,
            original["head_sha"],
            f"step 3\nTimeout waiting for {marker} to start\n",
        )
        t.seed_batch_pr("failure", "Failing after 61s")

        def replacement() -> list[dict]:
            return [
                b
                for b in t.batches()
                if b["state"] == "forming" and b["id"] != original["id"]
            ]

        wait_for(lambda: len(replacement()) == 1, what="the replacement batch")
        repl = replacement()[0]
        assert sorted(m["id"] for m in repl["members"]) == sorted(sids)
        assert repl["infra_retries"] == 1 and repl["not_before"], repl
        assert next(b for b in t.batches() if b["id"] == original["id"])[
            "state"
        ] == "red"
        # backed off: ticks keep running and no PR appears before not_before
        seen = ticks[0]
        wait_for(lambda: ticks[0] >= seen + 5, what="ticks inside the backoff")
        assert server.pr_creates == 1, "the replacement flushed before not_before"
        assert [b["state"] for b in t.batches() if b["id"] == repl["id"]] == [
            "forming"
        ]
        # past the backoff the SAME stories go out in one new PR
        offset[0] = 400.0
        wait_for(
            lambda: any(
                b["id"] == repl["id"] and b["state"] == "open" for b in t.batches()
            ),
            what="the replacement to open after not_before",
        )
        assert server.pr_creates == 2
        assert server.update_calls == 0
        assert not [b for b in t.batches() if b["parent_batch_id"] == original["id"]]
        for sid in sids:
            assert client.get(f"/api/v1/stories/{sid}").json()["status"] == "landing"
        assert any("infra timeout" in r.getMessage() for r in caplog.records)
        _assert_retired(server, original)
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


def test_timeout_is_red_and_logged_when_the_logs_api_is_missing(
    tmp_path, monkeypatch, caplog
):
    import logging

    server = _fake_train_gitea()
    client = None
    try:
        client, project, _repo, _bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        t = Train(client, server, project["id"])
        _run_story(client, project["id"])
        original = _depart(t, 1, what="the batch to open")
        caplog.set_level(logging.ERROR)
        server.actions_404 = True
        _fail_job_log(server, original["head_sha"], "Timeout waiting for test pod")
        t.seed_batch_pr("failure", "Failing after 61s")
        # treated as red: the first red burns the flake re-run
        wait_for(lambda: server.update_calls == 1, what="the red re-run")
        assert any(
            "cannot classify CI failure, job logs API unavailable" in r.getMessage()
            for r in caplog.records
        )
        assert not [b for b in t.batches() if b["not_before"]]
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


RUN_3549_ID = "tests/test_price_outlook_forecast_job.py::test_x"
RUN_3549_LOG = (
    "learner container failed (exit 1) after 179 s\n"
    f"2026-10-31T10:00:00Z FAILED {RUN_3549_ID} - AssertionError\n"
    "1 failed, 1621 passed\n"
    "timeout waiting for test pod\n"
)


def test_pytest_failures_unit():
    from app.train import _pytest_failures

    assert _pytest_failures("0 failed, 10 passed") is None
    assert _pytest_failures("learner container failed (exit 1) after 179 s") is None
    assert _pytest_failures("1 failed, 1621 passed") == []
    assert _pytest_failures(RUN_3549_LOG) == [RUN_3549_ID]
    assert _pytest_failures("FAILED a::b\nFAILED a::b - x\n  FAILED c::d") == [
        "a::b",
        "c::d",
    ]


def test_tests_failed_in_ci_bounces_a_one_member_batch(tmp_path, monkeypatch):
    server = _fake_train_gitea()
    client = None
    try:
        client, project, _repo, _bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        t = Train(client, server, project["id"])
        sid = _run_story(client, project["id"])
        batch = _depart(t, 1, what="the batch to open")
        _fail_job_log(server, batch["head_sha"], RUN_3549_LOG)
        t.seed_batch_pr("failure", "Failing after 61s")
        wait_for(
            lambda: client.get(f"/api/v1/stories/{sid}").json()["status"]
            == "human_review",
            what="the bounce",
        )
        assert server.update_calls == 0
        story = t.story(sid)
        assert RUN_3549_ID in story["error"]
        assert story["error"].startswith("land failed: tests failed in CI:")
        note = next(n for n in story["_notes"] if "failed_tests" in n)
        assert note["failed_tests"] == [RUN_3549_ID]
        assert note["reason"] == "tests failed in CI"
        row = next(b for b in t.batches() if b["id"] == batch["id"])
        assert row["red_runs"] == 1 and row["infra_retries"] == 0
        assert row["failed_tests"] == [RUN_3549_ID]
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


def test_tests_failed_in_ci_bisects_a_two_member_batch(tmp_path, monkeypatch):
    server = _fake_train_gitea()
    client = None
    try:
        client, project, _repo, _bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        t = Train(client, server, project["id"])
        sids = _run_stories_together(client, project["id"], 2)
        parent = _depart(t, 2, what="the batch to open")
        _fail_job_log(server, parent["head_sha"], RUN_3549_LOG)
        t.seed_batch_pr("failure", "Failing after 61s")
        wait_for(
            lambda: len([b for b in t.batches() if b["parent_batch_id"]]) == 2,
            what="the two child batches",
        )
        assert server.update_calls == 0
        row = next(b for b in t.batches() if b["id"] == parent["id"])
        assert row["state"] == "red" and row["red_runs"] == 1
        assert row["infra_retries"] == 0
        assert RUN_3549_ID in row["failed_tests"]
        assert all(
            c["parent_batch_id"] == parent["id"]
            for c in t.batches()
            if c["parent_batch_id"]
        )
        for sid in sids:
            assert client.get(f"/api/v1/stories/{sid}").json()["status"] != (
                "human_review"
            )
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


def test_timeout_without_pytest_output_is_still_an_infra_retry(tmp_path, monkeypatch):
    server = _fake_train_gitea()
    client = None
    try:
        client, project, _repo, _bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        t = Train(client, server, project["id"])
        _run_stories_together(client, project["id"], 2)
        original = _depart(t, 2, what="the batch to open")
        _fail_job_log(
            server, original["head_sha"], "Timeout waiting for test pod to start"
        )
        t.seed_batch_pr("failure", "Failing after 61s")
        wait_for(
            lambda: any(
                b["state"] == "forming" and b["id"] != original["id"]
                for b in t.batches()
            ),
            what="the infra replacement",
        )
        repl = next(
            b
            for b in t.batches()
            if b["state"] == "forming" and b["id"] != original["id"]
        )
        assert repl["infra_retries"] == 1 and repl["red_runs"] == 0
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


def test_a_project_runs_one_train_ci_at_a_time(tmp_path, monkeypatch):
    server = _fake_train_gitea()
    client = None
    try:
        client, project, _repo, _bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        ticks = _count_train_ticks(monkeypatch)
        # this test is about the per-project CI slot, not the scheduler hold:
        # the second story must still run while the first batch's CI is open
        from app import train as train_module

        monkeypatch.setattr(train_module, "train_ci_hold", lambda db: None)
        t = Train(client, server, project["id"])
        _run_story(client, project["id"])
        first = _depart(t, 1, what="the first batch to open")
        # a second batch becomes ready while the first one's CI is in flight
        _run_story(client, project["id"])
        wait_for(
            lambda: len([b for b in t.batches() if b["state"] == "forming"]) == 1,
            what="the second forming batch",
        )
        time.sleep(1.2)  # outlast the 1 s window so the batch is due
        seen = ticks[0]
        wait_for(lambda: ticks[0] >= seen + 5, what="ticks with the batch due")
        assert server.pr_creates == 1
        assert len([b for b in t.batches() if b["state"] == "forming"]) == 1
        # the first batch is closed by hand: the slot frees, the second goes
        t.seed_batch_pr("")
        server.pulls[first["pr_index"]] = {
            "merged": False,
            "mergeable": True,
            "state": "closed",
        }
        wait_for(lambda: server.pr_creates == 2, what="the second batch to flush")
        assert f"train/{first['id']}" in server.deleted_branches
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


# ---- conflicts -------------------------------------------------------------


def test_conflicting_story_is_left_out_and_the_rest_still_batch(
    tmp_path, monkeypatch
):
    server = _fake_train_gitea()
    client = None
    try:
        # each story writes different bytes to the SAME file: the second
        # branch cannot be merged into the batch branch (add/add conflict)
        client, project, repo, bare = _make_train(
            tmp_path,
            monkeypatch,
            server,
            window=1,
            tmax=6,
            gw=ScriptedGW(content=lambda n: f"VALUE = {n + 1}\n"),
        )
        t = Train(client, server, project["id"])
        clean = _run_story(client, project["id"])
        # a story whose branch conflicts with the first one on the same file
        conflicting = _run_story(client, project["id"])
        # both joined ONE batch (the hold kept it forming); the flush drops
        # the conflicting one, so the batch OPENS with a single member
        batch = _depart(t, 2, what="the batch to open", open_members=1)
        assert [m["id"] for m in batch["members"]] == [clean]
        # the conflicting story bounced to the gate with a conflict note
        wait_for(
            lambda: client.get(f"/api/v1/stories/{conflicting}").json()["status"]
            == "human_review",
            what="the conflict bounce",
        )
        gate = client.get(f"/api/v1/stories/{conflicting}").json()
        assert gate["error"] is not None
        assert "land conflict" in gate["error"]
        assert "feature.py" in gate["error"]
        notes = [
            json.loads(e.get("data") or "{}")
            for e in client.get(f"/api/v1/stories/{conflicting}").json().get(
                "events", []
            )
            if e.get("kind") == "note"
        ]
        conflict_notes = [n for n in notes if n.get("conflict")]
        assert conflict_notes, notes
        assert "feature.py" in conflict_notes[-1]["conflict"]
        # the rest still batched and landed
        assert [m["id"] for m in batch["members"]] == [clean]
        t.seed_batch_pr("success", "ok")
        server.pulls[batch["pr_index"]] = {
            "merged": True,
            "mergeable": True,
            "state": "open",
        }
        _git(repo, "fetch", "-q", "origin")
        _git(repo, "merge", "--ff-only", f"origin/train/{batch['id']}")
        _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(repo, "reset", "-q", "--hard", "origin/main")
        wait_for(
            lambda: client.get(f"/api/v1/stories/{clean}").json()["status"] == "done",
            what="the clean story to land",
        )
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


# ---- infra retry cap and operator unland -------------------------------------


def test_infra_retry_cap_bounces_the_batch_to_the_human_gate(tmp_path, monkeypatch):
    from datetime import timedelta

    from app import train as train_module

    # a fixed backoff plus a clock offset keeps every replacement observable
    # as FORMING until the test moves the clock past its not_before
    monkeypatch.setattr(train_module, "INFRA_RETRY_BASE_S", 60)
    monkeypatch.setattr(train_module, "INFRA_RETRY_MAX_S", 60)
    offset = [0.0]
    real_now = train_module.train_now
    monkeypatch.setattr(
        train_module,
        "train_now",
        lambda: real_now() + timedelta(seconds=offset[0]),
    )
    server = _fake_train_gitea()
    client = None
    try:
        client, project, _repo, _bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        t = Train(client, server, project["id"])
        sids = _run_stories_together(client, project["id"], 2)
        batch = _depart(t, 2, what="the batch to open")
        marker = "Timeout waiting for test pod to start"
        for n in range(1, 4):
            _fail_job_log(server, batch["head_sha"], f"step 3\n{marker}\n")
            t.seed_batch_pr("failure", "Failing after 61s")
            wait_for(
                lambda n=n: any(
                    b["state"] == "forming" and b["infra_retries"] == n
                    for b in t.batches()
                ),
                what=f"forming replacement #{n}",
            )
            forming = next(b for b in t.batches() if b["state"] == "forming")
            for sid in sids:
                assert client.get(f"/api/v1/stories/{sid}").json()["status"] == "landing"
            notes = [
                x for x in t.story(sids[0])["_notes"] if x.get("infra_retry") == n
            ]
            assert notes and notes[-1]["next_attempt_at"], notes
            assert notes[-1]["infra_retry_cap"] == 3
            assert f"infra retry {n}/3" in notes[-1]["reason"]
            _assert_retired(server, batch)
            offset[0] += 120.0
            wait_for(
                lambda f=forming: any(
                    b["id"] == f["id"] and b["state"] == "open" for b in t.batches()
                ),
                what=f"replacement #{n} to open",
            )
            batch = next(b for b in t.batches() if b["id"] == forming["id"])
        # fourth failure: cap reached
        _fail_job_log(server, batch["head_sha"], f"step 3\n{marker}\n")
        t.seed_batch_pr("failure", "Failing after 61s")
        for sid in sids:
            wait_for(
                lambda sid=sid: client.get(f"/api/v1/stories/{sid}").json()["status"]
                == "human_review",
                what="story bounced",
            )
        assert not [b for b in t.batches() if b["state"] == "forming"]
        story = t.story(sids[0])
        assert "infra retry cap (3) reached" in story["error"]
        note = next(x for x in story["_notes"] if x.get("infra_retry_cap") == 3 and "log_tail" in x)
        assert note["infra_retries"] == 3
        assert note["ci_failure"] == "Failing after 61s"
        assert marker in note["log_tail"]
        assert "cap" in note["reason"]
        _assert_retired(server, batch)
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


def test_unland_pulls_an_open_train_batch_back_to_the_gate(tmp_path, monkeypatch):
    server = _fake_train_gitea()
    client = None
    try:
        client, project, _repo, _bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        t = Train(client, server, project["id"])
        sids = _run_stories_together(client, project["id"], 2)
        batch = _depart(t, 2, what="the batch to open")
        t.seed_batch_pr("pending")
        r = client.post(f"/api/v1/stories/{sids[0]}/unland", json={"by": "ops"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "human_review"
        for sid in sids:
            assert client.get(f"/api/v1/stories/{sid}").json()["status"] == "human_review"
        assert next(b for b in t.batches() if b["id"] == batch["id"])["state"] == "red"
        _assert_retired(server, batch)
        # no longer landing: 409
        r = client.post(f"/api/v1/stories/{sids[0]}/unland")
        assert r.status_code == 409
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)
