"""Execution: worktree isolation, command permissions, and the coder cycle."""

import json
import subprocess
import threading
import time

import pytest

from app import workspaces
from app.agent_tools import ToolBox

from conftest import MINIMAL_SPEC, STORY_PHASE_MARKERS


def phase_of(messages: list[dict]) -> str | None:
    """Which story phase (`=== ... REQUEST ===` marker) this conversation
    belongs to: scan the conversation for the *latest* message whose string
    content starts with a phase marker.

    Stateful per conversation, stateless per fake: after a tool call the last
    message is a tool result (no marker), but scanning backwards still finds
    the coder/code-review marker that opened the round. Non-str content
    (lists/None) is skipped the same way conftest's `_is_story_phase` does.
    """
    found = None
    for message in messages or []:
        content = message.get("content")
        if not isinstance(content, str):
            continue
        for marker in STORY_PHASE_MARKERS:
            if content.startswith(marker):
                found = marker
    return found


def init_repo(tmp_path) -> str:
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


# ---- workspaces ----------------------------------------------------------


def test_worktree_lifecycle(tmp_path):
    repo = init_repo(tmp_path)
    wt, base, _new = workspaces.create(repo, "story1")
    assert wt.exists() and (wt / "README.md").exists()

    (wt / "feature.py").write_text("print('new')\n")
    diff = workspaces.diff(repo, "story1")
    assert "feature.py" in diff and "new" in diff
    assert workspaces.changes_exist(repo, "story1")

    out = workspaces.merge(repo, "story1")
    assert "story1" in out.lower() or "merge" in out.lower()
    assert (Path(repo := repo) / "feature.py").exists()

    workspaces.discard(repo, "story1")
    assert not wt.exists()
    assert not workspaces.changes_exist(repo, "story1")


def test_recreated_worktree_diffs_from_fork_point(tmp_path):
    """Regression: a retry that reuses the story worktree must keep diffing
    against the ORIGINAL fork point, not the branch head — otherwise work
    committed by earlier attempts reads as "no changes"."""
    repo = init_repo(tmp_path)
    wt, base1, _ = workspaces.create(repo, "s")
    assert base1 == subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    # first attempt commits work onto the story branch
    (wt / "feature.py").write_text("print('v1')\n")
    workspaces.commit(repo, "s", "attempt 1")
    assert workspaces.diff(repo, "s", base1).strip()

    # retry: create() again — base must still be the fork point, and the
    # previously committed work must still count as changes vs that base
    _, base2, _ = workspaces.create(repo, "s")
    assert base2 == base1, "reused worktree must report the original fork point"
    assert "feature.py" in workspaces.diff(repo, "s", base2)


from pathlib import Path  # noqa: E402


def _git_out(repo: str, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def test_conflicting_merge_aborts_and_names_files(tmp_path):
    """Two stories touching the same file: the second merge must fail with
    the conflicted file named, and leave main exactly as the first merge
    left it — no MERGE_HEAD, no staged conflicts, no half-merged content."""
    repo = init_repo(tmp_path)
    for sid, value in (("story-a", "first"), ("story-b", "second")):
        wt, _base, _new = workspaces.create(repo, sid)
        (wt / "feature.py").write_text(f"VALUE = '{value}'\n")

    out = workspaces.merge(repo, "story-a")
    assert "story-a" in out.lower() or "merge" in out.lower()
    assert (Path(repo) / "feature.py").read_text() == "VALUE = 'first'\n"
    clean_head = _git_out(repo, "rev-parse", "HEAD")

    with pytest.raises(workspaces.WorkspaceError) as ei:
        workspaces.merge(repo, "story-b")
    message = str(ei.value)
    assert "conflicting files:" in message
    assert "feature.py" in message
    assert "discard the story" in message

    # no half-merge residue on the main checkout (regression: MERGE_HEAD)
    assert not (Path(repo) / ".git" / "MERGE_HEAD").exists()
    assert _git_out(repo, "status", "--porcelain") == ""
    # main still holds the first story's version of the file
    assert (Path(repo) / "feature.py").read_text() == "VALUE = 'first'\n"
    assert _git_out(repo, "rev-parse", "HEAD") == clean_head
    # the failed story's worktree survives so a human can resolve it
    assert (Path(repo) / ".fubala-worktrees" / "story-b").exists()


def test_merge_is_idempotent_on_retry(tmp_path):
    """Re-approving an already-merged story is a success, never a duplicate
    merge commit — with the branch still present, and again after discard
    removed it."""
    repo = init_repo(tmp_path)
    wt, _base, _new = workspaces.create(repo, "story1")
    (wt / "feature.py").write_text("VALUE = 'executed'\n")

    head_before = _git_out(repo, "rev-parse", "HEAD")
    count_before = _git_out(repo, "rev-list", "--count", "HEAD")
    first = workspaces.merge(repo, "story1")
    assert first != "already merged"
    head_merged = _git_out(repo, "rev-parse", "HEAD")
    assert head_merged != head_before

    # branch still present (worktree not yet discarded)
    assert workspaces.merge(repo, "story1") == "already merged"
    # and again once discard has removed the branch
    workspaces.discard(repo, "story1")
    assert workspaces.merge(repo, "story1") == "already merged"

    assert _git_out(repo, "rev-parse", "HEAD") == head_merged
    # exactly one story commit + one merge commit, no matter how often
    # merge/approve is retried
    assert _git_out(repo, "rev-list", "--count", "HEAD") == str(int(count_before) + 2)
    assert (Path(repo) / "feature.py").read_text() == "VALUE = 'executed'\n"


def test_retry_with_legacy_worktree(tmp_path):
    """Pre-003 stories left their worktree OUTSIDE the repo
    (parent-of-repo). A retry must discard it and rebuild in-root from
    current main — never keep executing against the untrusted path."""
    repo = init_repo(tmp_path)
    story_id = "legacy1"
    old_head = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    # simulate the pre-fix state: worktree + branch at the old location
    legacy = tmp_path / ".fubala-worktrees" / story_id
    legacy.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", repo, "worktree", "add", "-b", f"fubala/{story_id}", str(legacy)],
        check=True, capture_output=True,
    )
    assert legacy.exists()

    # main moves on while the story waits for a retry
    (Path(repo) / "main.txt").write_text("advanced\n")
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "advance"],
        cwd=repo, check=True,
    )
    new_head = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    wt, base, created_new = workspaces.create(repo, story_id)
    assert not legacy.exists(), "legacy out-of-root worktree must be discarded"
    assert wt == Path(repo) / ".fubala-worktrees" / story_id
    assert wt.exists() and (wt / "main.txt").exists()
    assert created_new is True
    assert base == new_head, "rebuilt worktree must re-pin to the new main HEAD"
    assert base != old_head


def test_retry_with_stale_branch_only(tmp_path):
    """A branch that outlived its worktree (004) must not break
    `worktree add -b`; it is recreated at current HEAD."""
    repo = init_repo(tmp_path)
    story_id = "stale1"
    head = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "-C", repo, "branch", f"fubala/{story_id}"], check=True)

    wt, base, created_new = workspaces.create(repo, story_id)
    assert created_new is True
    assert base == head
    assert wt.exists()
    checked_out = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert checked_out == head


def test_retry_clean(tmp_path):
    """A healthy in-root worktree is REUSED: cleanup must never fire, the
    base stays the original fork point, and earlier committed work still
    shows in the diff against base_ref."""
    repo = init_repo(tmp_path)
    wt1, base1, created1 = workspaces.create(repo, "clean1")
    assert created1 is True

    (wt1 / "feature.py").write_text("print('attempt 1')\n")
    workspaces.commit(repo, "clean1", "attempt 1")

    wt2, base2, created2 = workspaces.create(repo, "clean1")
    assert wt2 == wt1 and created2 is False
    assert base2 == base1, "reused worktree keeps the original fork point"
    assert "feature.py" in workspaces.diff(repo, "clean1", base1)
    assert workspaces.changes_exist(repo, "clean1", base1)


def test_worktree_add_retries_once_on_branch_exists(tmp_path, monkeypatch):
    """If `worktree add -b` fails (branch resurrected mid-flight), one
    cleanup-and-retry must recover; a second failure surfaces the stderr."""
    repo = init_repo(tmp_path)
    real_git = workspaces._git
    calls = {"add": 0}

    def flaky_git(project_root, *args, cwd=None):
        if args[:2] == ("worktree", "add"):
            calls["add"] += 1
            if calls["add"] == 1:
                raise workspaces.WorkspaceError(
                    "git worktree add: fatal: a branch named 'fubala/x' already exists"
                )
        return real_git(project_root, *args, cwd=cwd)

    monkeypatch.setattr(workspaces, "_git", flaky_git)

    wt, base, created_new = workspaces.create(repo, "x")
    assert calls["add"] == 2, "first add fails, retry succeeds"
    assert created_new is True
    assert wt.exists() and (wt / "README.md").exists()
    assert base == workspaces.head(repo)


def test_worktree_add_gives_up_after_one_retry(tmp_path, monkeypatch):
    """One retry only — persistent failures (dirty path, permissions) must
    surface their stderr instead of being masked by cleanup."""
    repo = init_repo(tmp_path)
    real_git = workspaces._git
    stderr = "fatal: '<path>' already exists and is not an empty directory"

    def broken_git(project_root, *args, cwd=None):
        if args[:2] == ("worktree", "add"):
            raise workspaces.WorkspaceError(f"git worktree add: {stderr}")
        return real_git(project_root, *args, cwd=cwd)

    monkeypatch.setattr(workspaces, "_git", broken_git)

    with pytest.raises(workspaces.WorkspaceError, match="worktree add failed"):
        workspaces.create(repo, "y")


def test_non_git_root_rejected(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(workspaces.WorkspaceError, match="git"):
        workspaces.create(str(plain), "x")


# ---- command permissions ----------------------------------------------------


def test_run_command_allowlist_and_skip(tmp_path):
    repo = init_repo(tmp_path)
    box = ToolBox(
        engine=None,
        project_root=repo,
        workspace=repo,
        allow_commands=("python3", "git"),
        writable=True,
    )
    ok = box.execute("run_command", json.dumps({"command": "python3 -c 'print(1+1)'"}))
    assert "exit=0" in ok and "2" in ok

    denied = box.execute("run_command", json.dumps({"command": "rm -rf ."}))
    assert "not on the allowed commands list" in denied

    yolo = ToolBox(
        engine=None, project_root=repo, workspace=repo,
        allow_commands=(), skip_permissions=True, writable=True,
    )
    ran = yolo.execute("run_command", json.dumps({"command": "python3 -c 'print(42)'"}))
    assert "42" in ran


def test_write_file_jailed_and_deny(tmp_path):
    repo = init_repo(tmp_path)
    box = ToolBox(engine=None, project_root=repo, workspace=repo, writable=True)
    assert "wrote" in box.execute(
        "write_file", json.dumps({"path": "src/new.py", "content": "x = 1\n"})
    )
    assert (Path(repo) / "src" / "new.py").exists()
    assert "escapes the workspace" in box.execute(
        "write_file", json.dumps({"path": "../outside.txt", "content": "no"})
    )
    assert "sensitive-file policy" in box.execute(
        "write_file", json.dumps({"path": ".env", "content": "k=v"})
    )
    # read-only toolboxes (chat, spec writer) never offer or accept writes:
    # a spec-stage write leaking into the live checkout is the 003 bug
    readonly = ToolBox(engine=None, project_root=repo)
    assert "write_file" not in [t["function"]["name"] for t in readonly.schemas()]
    assert "no workspace" in readonly.execute(
        "write_file", json.dumps({"path": "leak.md", "content": "x"})
    )


# ---- full coder cycle -------------------------------------------------------


def test_full_execution_cycle(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "exec.db"))

    # scripted agent: spec -> ai review approve -> coder (write_file + text)
    # -> code review approve
    class ScriptedGateway:
        def __init__(self):
            self.calls = 0
            self.seen_tools = []

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import ToolCall as TC
            from app.providers import Usage as U

            self.calls += 1
            if self.calls == 1:  # spec
                yield D(text=MINIMAL_SPEC)
            if self.calls == 2:  # ai review of spec
                yield D(text="fine\n\nVERDICT: APPROVED")
            if self.calls == 3:  # coder: one write, then done
                assert any(t["function"]["name"] == "write_file" for t in tools or [])
                yield TC(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps(
                        {"path": "feature.py", "content": "VALUE = 'executed'\n"}
                    ),
                )
            if self.calls == 4:  # coder final text
                yield D(text="implemented feature.py; verified by reading it back")
            if self.calls == 5:  # code review
                yield D(text="diff matches spec\n\nVERDICT: APPROVED")
            yield U(prompt_tokens=1, completion_tokens=1)

    gateway = ScriptedGateway()
    with TestClient(create_app(gateway=gateway)) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "feature X"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")

        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed", "done"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")

        # approve the spec -> execution -> code review -> back to human review
        first = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"}).json()
        assert first["status"] == "executing"

        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed", "done"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")
        assert current["base_ref"]

        detail = client.get(f"/api/v1/stories/{story['id']}").json()
        assert detail["code_diff"] and "feature.py" in detail["code_diff"]

        # final approve merges the worktree into the repo
        final = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"}).json()
        assert final["status"] == "done"
        assert (Path(repo) / "feature.py").read_text() == "VALUE = 'executed'\n"
        assert not (Path(repo) / ".fubala-worktrees" / story["id"]).exists()


def test_approve_conflict_returns_409_and_keeps_story_in_review(tmp_path, monkeypatch):
    """Merge safety (endpoint): a conflicting approve is a 409 naming the
    conflicted file, the story stays in human_review, and main keeps the
    first story's version with no MERGE_HEAD residue."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "conflict.db"))

    # the first (winning) story is plain git work: merge it before the app
    # boots so the app never sees its worktree
    wt, _base, _new = workspaces.create(repo, "story-a")
    (wt / "feature.py").write_text("VALUE = 'first'\n")
    workspaces.merge(repo, "story-a")
    workspaces.discard(repo, "story-a")
    clean_head = _git_out(repo, "rev-parse", "HEAD")

    class GW:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import ToolCall as TC
            from app.providers import Usage as U

            self.calls += 1
            if self.calls == 1:  # spec
                yield D(text=MINIMAL_SPEC)
            elif self.calls == 2:  # ai review
                yield D(text="VERDICT: APPROVED")
            elif self.calls == 3:  # coder writes the conflicting file
                yield TC(id="w", name="write_file",
                         arguments=json.dumps({"path": "feature.py",
                                               "content": "VALUE = 'second'\n"}))
            elif self.calls == 4:
                yield D(text="done")
            else:  # code review
                yield D(text="VERDICT: APPROVED")
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

    with TestClient(create_app(gateway=GW())) as client:
        client.patch("/api/v1/settings", json={
            "story_poll_interval": 0.05, "project_root": repo,
            "write_tools_enabled": True,
        })
        story = client.post("/api/v1/stories", json={"title": "feature"}).json()
        sid = story["id"]
        client.post(f"/api/v1/stories/{sid}/queue")
        # spec -> ai review -> human review
        wait_status(client, sid, ("human_review", "failed"))
        client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
        current = wait_status(client, sid, ("human_review", "failed", "done"))
        assert current["status"] == "human_review", current.get("error")
        assert current["base_ref"]

        # main advances the SAME file after the story forked -> the approve
        # merge is now a real conflict
        (Path(repo) / "feature.py").write_text("VALUE = 'main'\n")
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", repo,
             "commit", "-qm", "main progress on feature.py"], check=True,
        )
        clean_head = _git_out(repo, "rev-parse", "HEAD")

        # the conflicting approve: 409 naming the file, story untouched
        res = client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
        assert res.status_code == 409
        detail = res.json()["detail"]
        assert detail.startswith("merge failed: ")
        assert "conflicting files: feature.py" in detail
        assert "discard the story" in detail

        after = client.get(f"/api/v1/stories/{sid}").json()
        assert after["status"] == "human_review"
        assert not (Path(repo) / ".git" / "MERGE_HEAD").exists()
        assert _git_out(repo, "status", "--porcelain") == ""
        assert (Path(repo) / "feature.py").read_text() == "VALUE = 'main'\n"
        assert _git_out(repo, "rev-parse", "HEAD") == clean_head
        # the failed story's worktree survives for manual resolution
        assert (Path(repo) / ".fubala-worktrees" / sid).exists()


def test_slow_coder_not_re_entered_by_fast_ticks(tmp_path, monkeypatch):
    """A coder still in flight must not be picked up again by the next tick:
    regression for duplicate concurrent coders (multiple worktree notes and
    gateway calls for one story) when story_poll_interval is small."""
    import asyncio
    import sqlite3

    from fastapi.testclient import TestClient

    from app.main import create_app

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "exec2.db"))

    class SlowGateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import ToolCall as TC
            from app.providers import Usage as U

            self.calls += 1
            if self.calls == 1:  # spec
                yield D(text=MINIMAL_SPEC)
            elif self.calls == 2:  # ai review
                yield D(text="ok\n\nVERDICT: APPROVED")
            elif self.calls == 3:  # coder: slow enough for several ticks to fire
                await asyncio.sleep(0.8)
                yield TC(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps({"path": "thing.py", "content": "X = 1\n"}),
                )
            elif self.calls == 4:
                yield D(text="wrote thing.py")
            elif self.calls == 5:  # code review
                await asyncio.sleep(0.8)
                yield D(text="good\n\nVERDICT: APPROVED")
            yield U(prompt_tokens=1, completion_tokens=1)

    with TestClient(create_app(gateway=SlowGateway())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "thing"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")

        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")

        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed", "done"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")

        # exactly one coder run: one worktree note, five gateway calls total
        db = sqlite3.connect(tmp_path / "exec2.db")
        worktree_notes = db.execute(
            "SELECT COUNT(*) FROM story_events WHERE kind='note' AND data LIKE '%worktree%'"
        ).fetchone()[0]
        db.close()
        assert worktree_notes == 1, f"{worktree_notes} worktree notes — coder re-entered"


def test_watchdog_cancels_and_requeues_hung_stream(tmp_path, monkeypatch):
    """A run whose event stream goes silent past WATCHDOG_TIMEOUT must be
    cancelled and requeued with the watchdog note — no operator restart.

    Regression: the hung asyncio task used to occupy its slot forever."""
    import asyncio
    import sqlite3

    from fastapi.testclient import TestClient

    from app import runner as runner_module
    from app.main import create_app

    repo = init_repo(tmp_path)
    db_path = tmp_path / "watchdog.db"
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
    monkeypatch.setattr(runner_module, "WATCHDOG_TIMEOUT", 0.5)

    class HungGateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import Usage as U

            self.calls += 1
            # one chunk, then silence forever: the run is alive but mute
            yield D(text=MINIMAL_SPEC)
            await asyncio.sleep(3600)
            yield U(prompt_tokens=1, completion_tokens=1)  # pragma: no cover

    gateway = HungGateway()
    with TestClient(create_app(gateway=gateway)) as client:
        client.patch(
            "/api/v1/settings",
            json={"story_poll_interval": 0.1, "project_root": repo},
        )
        story = client.post("/api/v1/stories", json={"title": "hangs"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")

        note = None
        deadline = time.time() + 30
        while time.time() < deadline:
            db = sqlite3.connect(db_path)
            rows = db.execute(
                "SELECT data FROM story_events "
                "WHERE kind='status' AND data LIKE '%watchdog%'"
            ).fetchall()
            db.close()
            if rows:
                note = rows[0][0]
                break
            time.sleep(0.1)

        assert note, "no watchdog requeue event recorded"
        assert "watchdog: stream hung, requeued" in note
        # requeued means picked up again: a second gateway call happened.
        # The pickup rides the story poll loop, so it can land a tick after
        # the requeue event is written — under CI load the fixed sleeps
        # above routinely burn the budget before that next poll fires.
        deadline = time.time() + 30
        while gateway.calls < 2 and time.time() < deadline:
            time.sleep(0.1)
        assert gateway.calls >= 2, (
            f"{gateway.calls} gateway calls — story never picked up again"
        )

        # CancelledError must not be converted into a _fail write by the
        # pipeline's `except Exception` handlers
        current = client.get(f"/api/v1/stories/{story['id']}").json()
        assert current["status"] != "failed", current.get("error")


def test_verify_stage_runs_fix_round_and_gates_merge(tmp_path, monkeypatch):
    """Verify command fails first (marker file missing), the coder fixes it in
    a bounded fix round, verify passes, and the story carries the result."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "verify.db"))

    class ScriptedGateway:
        def __init__(self):
            self.calls = 0
            self.prompts = []

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import ToolCall as TC
            from app.providers import Usage as U

            self.calls += 1
            self.prompts.append(messages[-1]["content"])
            if self.calls == 1:  # spec
                yield D(text=MINIMAL_SPEC)
            elif self.calls == 2:  # ai review
                yield D(text="fine\n\nVERDICT: APPROVED")
            elif self.calls == 3:  # coder: writes the wrong thing
                yield TC(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps({"path": "feature.py", "content": "X = 1\n"}),
                )
            elif self.calls == 4:
                yield D(text="done")
            elif self.calls == 5:  # fix round: create the marker verify wants
                assert messages[-1]["content"].startswith("=== FIX REQUEST ===")
                assert "Verification is failing" in messages[-1]["content"]
                yield TC(
                    id="w2",
                    name="write_file",
                    arguments=json.dumps({"path": "fixed.marker", "content": "ok\n"}),
                )
            elif self.calls == 6:
                yield D(text="fixed by adding the marker")
            elif self.calls == 7:  # code review
                yield D(text="diff matches spec\n\nVERDICT: APPROVED")
            yield U(prompt_tokens=1, completion_tokens=1)

    gateway = ScriptedGateway()
    with TestClient(create_app(gateway=gateway)) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "max_tool_calls": 8,
                "verify_command": "test -f fixed.marker",
                "verify_fix_rounds": 2,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "marker"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")

        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")

        # approve the spec -> coder runs -> verify -> fix -> code review
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed", "done"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")

        v = current["verify_result"]
        assert v["passed"] is True
        assert v["attempts"] == 2  # first failed, fix round made it pass
        assert v["command"] == "test -f fixed.marker"
        # fix-round work landed in the worktree
        assert (Path(repo) / ".fubala-worktrees" / story["id"] / "fixed.marker").exists()

        # code reviewer was told about verification
        assert "PASSED" in gateway.prompts[-1] or "Verification" in gateway.prompts[-1]

        # merge succeeds now; verify-failed stories are blocked (next test)
        final = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"}).json()
        assert final["status"] == "done"


def test_merge_gate_blocks_failed_verify(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "gate.db"))

    class MinimalGateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import Usage as U

            self.calls += 1
            if self.calls == 1:
                yield D(text=MINIMAL_SPEC)
            elif self.calls == 2:
                yield D(text="VERDICT: APPROVED")
            elif self.calls == 3:  # coder writes something, verify still fails
                from app.providers import ToolCall as TC

                yield TC(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps({"path": "x.py", "content": "x = 1\n"}),
                )
            elif self.calls == 4:
                yield D(text="done")
            elif self.calls == 5:
                yield D(text="VERDICT: APPROVED")
            yield U(prompt_tokens=1, completion_tokens=1)

    with TestClient(create_app(gateway=MinimalGateway())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "verify_command": "false",  # always fails
                "verify_fix_rounds": 0,  # no fix rounds: one attempt only
            },
        )
        story = client.post("/api/v1/stories", json={"title": "gate"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")

        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")

        # approve the spec so the coder (and always-failing verify) runs
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed", "done"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")
        assert current["verify_result"]["passed"] is False

        res = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        assert res.status_code == 409
        assert "verification failed" in res.json()["detail"]

        # with verification unconfigured the gate is open again
        client.patch("/api/v1/settings", json={"verify_command": ""})
        res = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        assert res.status_code == 200 and res.json()["status"] == "done"


def test_exit_zero_with_failed_counts_is_not_passed(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "mismatch.db"))

    class MinimalGateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import Usage as U

            self.calls += 1
            if self.calls == 1:
                yield D(text=MINIMAL_SPEC)
            elif self.calls == 2:
                yield D(text="VERDICT: APPROVED")
            elif self.calls == 3:  # coder writes something, verify still fails
                from app.providers import ToolCall as TC

                yield TC(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps({"path": "x.py", "content": "x = 1\n"}),
                )
            elif self.calls == 4:
                yield D(text="done")
            elif self.calls == 5:
                yield D(text="VERDICT: APPROVED")
            yield U(prompt_tokens=1, completion_tokens=1)

    with TestClient(create_app(gateway=MinimalGateway())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "verify_command": "printf '3 failed, 10 passed in 1.0s\\n'; exit 0",
                "verify_fix_rounds": 0,  # no fix rounds: one attempt only
            },
        )
        story = client.post("/api/v1/stories", json={"title": "gate"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")

        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")

        # approve the spec so the coder (and always-failing verify) runs
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed", "done"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")
        vr = current["verify_result"]
        assert vr["passed"] is False and vr["exit_code"] == 0
        assert "exit 0" in vr["mismatch"] and "3 failed" in vr["mismatch"]
        assert vr["counts"]["totals"]["passed"] == 10
        events = [
            json.loads(e["data"]) for e in current["events"] if e["kind"] == "verify"
        ]
        assert events and "3 failed" in events[-1]["mismatch"]
        assert events[-1]["passed"] is False


def test_fix_round_gets_fresh_call_budget(tmp_path, monkeypatch):
    """AC1: a fix round must start with a full tool-call budget. Round 1
    spends every one of tools_max_calls calls; the fix round's read_file
    still returns real content instead of "tool call budget exhausted"."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "budget1.db"))

    class ScriptedGateway:
        def __init__(self):
            self.calls = 0
            self.msgs = []

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import ToolCall as TC
            from app.providers import Usage as U

            self.calls += 1
            self.msgs.append(messages)
            if self.calls == 1:  # spec
                yield D(text=MINIMAL_SPEC)
            elif self.calls == 2:  # ai review
                yield D(text="fine\n\nVERDICT: APPROVED")
            elif self.calls == 3:  # coder round 1: spend ALL the calls
                yield TC(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps({"path": "a.py", "content": "a = 1\n"}),
                )
                yield TC(
                    id="w2",
                    name="write_file",
                    arguments=json.dumps({"path": "b.py", "content": "b = 2\n"}),
                )
            elif self.calls == 4:
                yield D(text="spent the budget, answering\n")
            elif self.calls == 5:  # fix round: read a file for real
                assert messages[-1]["content"].startswith("=== FIX REQUEST ===")
                yield TC(
                    id="r1",
                    name="read_file",
                    arguments=json.dumps({"path": "README.md"}),
                )
            elif self.calls == 6:
                yield D(text="read it, done\n")
            elif self.calls == 7:  # code review
                yield D(text="fine\n\nVERDICT: APPROVED")
            yield U(prompt_tokens=1, completion_tokens=1)

    gateway = ScriptedGateway()
    with TestClient(create_app(gateway=gateway)) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "tools_max_calls": 2,  # round 1 spends both
                "verify_command": "false",  # always red -> one fix round
                "verify_fix_rounds": 1,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "budget"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")

        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})

        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed", "done"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")

        # fix round = gateway calls 5 and 6 (the same growing message list);
        # find it by its FIX REQUEST prompt, not by call index
        assert gateway.calls >= 6, f"only {gateway.calls} gateway calls"
        fix_rounds = [
            m
            for m in gateway.msgs
            if any(
                isinstance(x.get("content"), str)
                and x["content"].startswith("=== FIX REQUEST ===")
                for x in m
            )
        ]
        assert fix_rounds, "no fix round ran at all"
        tool_msgs = [m for m in fix_rounds[-1] if m.get("role") == "tool"]
        assert tool_msgs, "fix round saw no tool result at all"
        content = tool_msgs[0]["content"]
        assert "exhausted" not in content, content
        assert content.startswith("[tool output"), content
        # it really read the repo README committed at init
        assert "# demo" in content


def test_fix_round_gets_fresh_time_budget(tmp_path, monkeypatch):
    """AC2: a fix round must start with a fresh time budget. Round 1 runs
    against tools_timeout_seconds=2, then a verify command sleeping past it;
    the fix round's first tool call is not refused with the
    "tool time budget exhausted" message."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "budget2.db"))

    class ScriptedGateway:
        def __init__(self):
            self.calls = 0
            self.msgs = []

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import ToolCall as TC
            from app.providers import Usage as U

            self.calls += 1
            self.msgs.append(messages)
            if self.calls == 1:  # spec
                yield D(text=MINIMAL_SPEC)
            elif self.calls == 2:  # ai review
                yield D(text="fine\n\nVERDICT: APPROVED")
            elif self.calls == 3:  # coder round 1: one write inside the budget
                yield TC(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps({"path": "feature.py", "content": "x = 1\n"}),
                )
            elif self.calls == 4:
                yield D(text="done\n")
            elif self.calls == 5:  # fix round: one read
                assert messages[-1]["content"].startswith("=== FIX REQUEST ===")
                yield TC(
                    id="r1",
                    name="read_file",
                    arguments=json.dumps({"path": "README.md"}),
                )
            elif self.calls == 6:
                yield D(text="read it, done\n")
            elif self.calls == 7:  # code review
                yield D(text="fine\n\nVERDICT: APPROVED")
            yield U(prompt_tokens=1, completion_tokens=1)

    gateway = ScriptedGateway()
    with TestClient(create_app(gateway=gateway)) as client:
        # the API validator floors tools_timeout_seconds at 5s; write the
        # round budget directly so this test stays fast
        from app.settings_store import save_settings

        save_settings(
            client.app.state.engine,
            {
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                # 2s, not a bare 0.05s: the fresh fix-round budget must
                # survive xdist CPU jitter between guarded.reset() and the
                # first tool execution (~10 event-loop/thread hops), while
                # the verify sleep below must still outlive the ROUND-1
                # deadline so a missing reset() stays detected. 2s budget
                # vs 4s sleep leaves >=2s margin on both sides.
                "tools_timeout_seconds": 2.0,
                # outlives the round-1 budget AND exits nonzero, so a fix
                # round really runs after the old deadline has expired
                "verify_command": "sleep 4; false",
                "verify_fix_rounds": 1,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "clocks"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")

        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})

        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed", "done"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")

        # fix round = gateway calls 5 and 6; only its FIRST call is asserted.
        # Located by its FIX REQUEST prompt, not by call index.
        assert gateway.calls >= 5, f"only {gateway.calls} gateway calls"
        fix_rounds = [
            m
            for m in gateway.msgs
            if any(
                isinstance(x.get("content"), str)
                and x["content"].startswith("=== FIX REQUEST ===")
                for x in m
            )
        ]
        assert fix_rounds, "no fix round ran at all"
        tool_msgs = [m for m in fix_rounds[-1] if m.get("role") == "tool"]
        assert tool_msgs, "fix round saw no tool result at all"
        content = tool_msgs[0]["content"]
        assert "tool time budget exhausted" not in content, content
        assert content.startswith("[tool output"), content


def test_red_verify_forces_code_review_rejection(tmp_path, monkeypatch):
    """A red verify must never reach the human gate as ai_verdict="approved",
    even when the code reviewer replies VERDICT: APPROVED (fix rounds already
    exhausted). The story lands at human_review marked rejected, with a note
    pointing at the failing verify output; the merge gate then still demands
    an explicit override to merge red code."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "gate.db"))

    class MinimalGateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import Usage as U

            self.calls += 1
            if self.calls == 1:
                yield D(text=MINIMAL_SPEC)
            elif self.calls == 2:
                yield D(text="VERDICT: APPROVED")
            elif self.calls == 3:  # coder writes something, verify still fails
                from app.providers import ToolCall as TC

                yield TC(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps({"path": "x.py", "content": "x = 1\n"}),
                )
            elif self.calls == 4:
                yield D(text="done")
            elif self.calls == 5:  # code reviewer approves — but verify is red
                yield D(text="VERDICT: APPROVED")
            yield U(prompt_tokens=1, completion_tokens=1)

    with TestClient(create_app(gateway=MinimalGateway())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "verify_command": "false",  # always fails
                "verify_fix_rounds": 0,  # no fix rounds: one attempt only
            },
        )
        story = client.post("/api/v1/stories", json={"title": "gate"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")

        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")

        # approve the spec so the coder (and always-failing verify) runs
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed", "done"):
                break
            time.sleep(0.05)
        # red verify + "VERDICT: APPROVED" must arrive rejected, never approved
        assert current["status"] == "human_review", current.get("error")
        assert current["verify_result"]["passed"] is False
        assert current["ai_verdict"] == "rejected"
        assert current["ai_verdict"] != "approved"

        detail = client.get(f"/api/v1/stories/{story['id']}").json()
        reasons = [
            json.loads(e["data"]).get("reason", "")
            for e in detail["events"]
            if e["kind"] == "note"
        ]
        assert any(
            "verify failed after 1 attempts" in r for r in reasons
        ), f"missing verify-failed marker note, got: {reasons}"

        # the merge gate still refuses to land red code without an override
        res = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        assert res.status_code == 409
        assert "verification failed" in res.json()["detail"]

        # an explicit, justified override is the only way through
        res = client.post(
            f"/api/v1/stories/{story['id']}/approve",
            json={"by": "t", "override_verify": True, "justification": "demo repo"},
        )
        assert res.status_code == 200 and res.json()["status"] == "done"


def test_missing_verdict_twice_rejects_at_human_gate(tmp_path, monkeypatch):
    """Test A: a code review with no parsable VERDICT line — even after its
    single corrective retry — must not land the story as ai_verdict="approved".
    It arrives at human_review marked rejected with a note saying so."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers import ToolCall

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "noverdict_a.db"))

    narration = "The change is small and readable.|No obvious defects spotted."

    class MinimalGateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import Usage as U

            self.calls += 1
            if self.calls == 1:
                yield D(text=MINIMAL_SPEC)
            elif self.calls == 2:
                yield D(text="VERDICT: APPROVED")
            elif self.calls == 3:  # coder writes something so the diff is real
                yield ToolCall(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps({"path": "x.py", "content": "x = 1\n"}),
                )
            elif self.calls == 4:
                yield D(text="done")
            else:  # 5 = review, 6 = retry: both verdict-less
                yield D(text=narration)
            yield U(prompt_tokens=1, completion_tokens=1)

    gw = MinimalGateway()
    with TestClient(create_app(gateway=gw)) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "gate"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")

        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")

        # approve the spec so the coder + code review run
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed", "done"):
                break
            time.sleep(0.05)

        assert current["status"] == "human_review", current.get("error")
        assert current["ai_verdict"] == "rejected", current
        assert current["ai_verdict"] != "approved"
        # exactly one corrective retry, never more
        assert gw.calls == 6, f"expected 6 gateway calls, got {gw.calls}"

        detail = client.get(f"/api/v1/stories/{story['id']}").json()
        notes = [
            json.loads(e["data"])
            for e in detail["events"]
            if e["kind"] == "note"
        ]
        assert {
            "reason": "code review produced no verdict",
            "by": "code-reviewer",
        } in notes, f"missing no-verdict note, got: {notes}"
        # the human sees the original reply and the retry, clearly separated
        assert "## Retry after missing verdict" in (current["ai_review"] or "")


def test_missing_verdict_retry_recovers_approval(tmp_path, monkeypatch):
    """Test B: the first code review carries no verdict, the corrective retry
    replies 'VERDICT: APPROVED' — the story then reaches human_review approved,
    with exactly one retry whose messages carry the nudge and the prior reply."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers import ToolCall

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "noverdict_b.db"))

    class MinimalGateway:
        def __init__(self):
            self.calls: list[dict] = []

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import Usage as U

            self.calls.append({"model": model, "messages": messages})
            n = len(self.calls)
            if n == 1:
                yield D(text=MINIMAL_SPEC)
            elif n == 2:
                yield D(text="VERDICT: APPROVED")
            elif n == 3:
                yield ToolCall(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps({"path": "x.py", "content": "x = 1\n"}),
                )
            elif n == 4:
                yield D(text="done")
            elif n == 5:  # first review: narration only, no verdict
                yield D(text="Looks fine overall.|No comments.")
            else:  # retry: verdict recovered
                yield D(text="VERDICT: APPROVED")
            yield U(prompt_tokens=1, completion_tokens=1)

    gw = MinimalGateway()
    with TestClient(create_app(gateway=gw)) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "gate"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")

        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")

        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed", "done"):
                break
            time.sleep(0.05)

        assert current["status"] == "human_review", current.get("error")
        assert current["ai_verdict"] == "approved", current

        review_calls = [
            c
            for c in gw.calls
            if "=== CODE REVIEW REQUEST" in c["messages"][1]["content"]
        ]
        assert len(review_calls) == 2, f"expected 2 review calls, got {len(review_calls)}"
        retry_messages = review_calls[1]["messages"]
        # multi-turn nudge: system + original request + prior reply + nudge
        assert retry_messages[2]["role"] == "assistant"
        assert "Looks fine overall." in retry_messages[2]["content"]
        nudge = retry_messages[3]["content"]
        assert retry_messages[3]["role"] == "user"
        assert "VERDICT: APPROVED" in nudge and "VERDICT: REJECTED" in nudge
        assert "exactly one line" in nudge
        assert "## Retry after missing verdict" in (current["ai_review"] or "")


def test_worktree_inside_project_root_and_trusted(tmp_path):
    """AC1+AC2 (fubala-prompts/003): the worktree lives inside the project
    root, .fubala-worktrees is locally excluded from git status, and the
    coder sandbox (ask mode) writes into the worktree without a permission
    prompt — while writes outside worktree+root stay refused (AC3)."""
    import asyncio

    from app.guardrails import GuardedExecutor, ToolPolicy

    repo = init_repo(tmp_path)
    wt, base, _ = workspaces.create(repo, "s1")
    assert str(wt).startswith(str(repo)), "worktree must live inside the project"
    exclude = (Path(repo) / ".git" / "info" / "exclude").read_text()
    assert ".fubala-worktrees" in exclude
    status = subprocess.run(
        ["git", "-C", repo, "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert ".fubala-worktrees" not in status, "worktrees must not pollute git status"

    policy = ToolPolicy(write_tools_enabled=True, permission_mode="ask")
    box = ToolBox(
        engine=None, project_root=str(wt), workspace=str(wt),
        allow_commands=policy.allowed_commands, extra_roots=[repo], writable=True,
    )
    guarded = GuardedExecutor(box, policy)

    async def call(name, args):
        return await guarded(name, json.dumps(args))

    inside = asyncio.run(call("write_file", {"path": "feature.py", "content": "x = 1\n"}))
    assert "wrote" in inside, f"worktree write refused in ask mode: {inside}"
    assert (wt / "feature.py").read_text() == "x = 1\n"
    diff = workspaces.diff(repo, "s1", base)
    assert "feature.py" in diff  # non-empty worktree-vs-base diff (AC2)

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    denied = asyncio.run(call(
        "write_file", {"path": str(outside / "evil.py"), "content": "x"}
    ))
    assert "denied" in denied or "escapes" in denied  # jail intact (AC3)
    assert not (outside / "evil.py").exists()

    workspaces.discard(repo, "s1")


def test_retry_with_legacy_worktree_rebuilds_in_root(tmp_path):
    """fubala-prompts/004: a pre-fix parent-of-repo worktree is discarded and
    rebuilt in-root from current main; a stale branch alone doesn't break
    creation; a fresh create reports created_new for base re-pinning."""
    # commit something on main AFTER the legacy worktree was forked, so we
    # can prove the rebuild bases on current main, not the stale pin
    repo = init_repo(tmp_path)
    legacy = Path(repo).parent / ".fubala-worktrees" / "old1"
    subprocess.run(
        ["git", "-C", repo, "worktree", "add", "-b", "fubala/old1", str(legacy), "HEAD"],
        check=True, capture_output=True,
    )
    (Path(repo) / "later.txt").write_text("progress on main\n")
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", repo, "commit", "-qm", "later"],
        check=True,
    )
    current = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    wt, base, created_new = workspaces.create(repo, "old1")
    assert not legacy.exists(), "legacy worktree must be discarded"
    assert str(wt).startswith(str(repo)), "rebuilt worktree must be in-root"
    assert created_new is True
    assert base == current, "rebuild must base on current main, not the stale pin"
    assert (wt / "later.txt").exists()

    # stale branch only (worktree removed, branch left behind)
    workspaces.discard(repo, "old1")
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", repo, "branch", "fubala/old1", "HEAD"],
        check=True, capture_output=True,
    )
    wt2, base2, created2 = workspaces.create(repo, "old1")
    assert created2 is True and str(wt2).startswith(str(repo))
    workspaces.discard(repo, "old1")


def test_deleting_a_story_cleans_its_worktree(tmp_path, monkeypatch):
    """AC4: DELETE /stories/{id} leaves no worktree behind."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "del.db"))

    class GW:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import Usage as U

            self.calls += 1
            if self.calls == 1:
                yield D(text=MINIMAL_SPEC)
            elif self.calls == 2:
                yield D(text="VERDICT: APPROVED")
            elif self.calls == 3:
                from app.providers import ToolCall as TC

                yield TC(id="w", name="write_file",
                         arguments=json.dumps({"path": "t.py", "content": "t = 1\n"}))
            elif self.calls == 4:
                yield D(text="done")
            elif self.calls == 5:
                yield D(text="VERDICT: APPROVED")
            yield U(prompt_tokens=1, completion_tokens=1)

    with TestClient(create_app(gateway=GW())) as client:
        client.patch("/api/v1/settings", json={
            "story_poll_interval": 0.05, "project_root": repo,
            "write_tools_enabled": True,
        })
        story = client.post("/api/v1/stories", json={"title": "cleanup"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        deadline = time.time() + 30
        while time.time() < deadline:
            cur = client.get(f"/api/v1/stories/{story['id']}").json()
            if cur["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert cur["status"] == "human_review", cur.get("error")

        # approve the spec so the coder creates the worktree
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        deadline = time.time() + 30
        while time.time() < deadline:
            cur = client.get(f"/api/v1/stories/{story['id']}").json()
            if cur["status"] in ("human_review", "failed", "done"):
                break
            time.sleep(0.05)
        assert cur["status"] == "human_review", cur.get("error")
        wt = Path(repo) / ".fubala-worktrees" / story["id"]
        assert wt.exists()

        res = client.delete(f"/api/v1/stories/{story['id']}")
        assert res.status_code == 204
        assert not wt.exists(), "deleted story must not leave a worktree behind"


def test_stale_base_ref_never_leaks_merged_main_into_diff(tmp_path, monkeypatch):
    """fubala-prompts/005 AC2: when a story's worktree is rebuilt (manual
    removal, legacy cleanup), the pinned base_ref is re-pinned to the new
    creation base — already-merged main commits can never masquerade as the
    coder's output in code_diff."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "stale.db"))

    class GW:
        """Marker-dispatched fake: the reply is chosen by the phase the
        conversation is in, never by call count, so poller timing (reject →
        retry / "no changes" requeue interleaving) cannot shift the cycle and
        hand spec text to a coder round. Stateless per prompt: a requeue
        re-sends `=== CODE REQUEST ===` and the coder writes feature.py again.
        """

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import ToolCall as TC
            from app.providers import Usage as U

            phase = phase_of(messages)
            last = messages[-1].get("content") if messages else None
            if phase == "=== SPEC REQUEST ===":
                yield D(text=MINIMAL_SPEC)
            elif phase in ("=== AI REVIEW REQUEST ===", "=== CODE REVIEW REQUEST ==="):
                yield D(text="VERDICT: APPROVED")
            elif phase == "=== CODE REQUEST ===":
                if isinstance(last, str) and last.startswith("=== CODE REQUEST ==="):
                    # fresh coder round: write the feature
                    yield TC(id="w", name="write_file",
                             arguments=json.dumps({"path": "feature.py", "content": "X = 1\n"}))
                else:
                    # tool-result round: wrap up the round
                    yield D(text="done")
            else:
                yield D(text="done")
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

    with TestClient(create_app(gateway=GW())) as client:
        client.patch("/api/v1/settings", json={
            "story_poll_interval": 0.05, "project_root": repo,
            "write_tools_enabled": True,
        })
        sid = client.post("/api/v1/stories", json={"title": "feature"}).json()["id"]
        client.post(f"/api/v1/stories/{sid}/queue")
        wait_status(client, sid, ("human_review", "failed"))
        client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
        first = wait_status(client, sid, ("human_review", "failed"))
        assert first["status"] == "human_review", first.get("error")
        stale_pin = first["base_ref"]

        # simulate the 005 scenario: worktree removed manually, main advances
        # with a merge of unrelated work, story requeued
        workspaces.discard(repo, sid)
        (Path(repo) / "unrelated_main_change.txt").write_text("merged work\n")
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", repo,
             "commit", "-qm", "unrelated main progress"], check=True,
        )
        client.post(f"/api/v1/stories/{sid}/reject", json={"reason": "retry"})

        # the reject requeues with base_ref set, so the retry goes straight
        # to the coder and back to human review
        final = wait_status(client, sid, ("human_review", "failed"))

        assert final["status"] == "human_review", final.get("error")
        assert final["base_ref"] != stale_pin, "base_ref must be re-pinned on rebuild"
        diff = final.get("code_diff") or ""
        assert "unrelated_main_change" not in diff, (
            "already-merged main commits leaked into the story's code_diff"
        )
        assert "feature.py" in diff, "the coder's own work must be the diff"


def test_project_verify_command_overrides_app_default(tmp_path, monkeypatch):
    """A project can carry its own verify command (repos differ: backend/
    vs backend_root/); it wins over the app-wide setting for its stories."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "pv.db"))

    class GW:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import ToolCall as TC
            from app.providers import Usage as U

            self.calls += 1
            if self.calls == 1:
                yield D(text=MINIMAL_SPEC)
            elif self.calls == 2:
                yield D(text="VERDICT: APPROVED")
            elif self.calls == 3:
                yield TC(id="w", name="write_file",
                         arguments=json.dumps({"path": "m.py", "content": "m = 1\n"}))
            elif self.calls == 4:
                yield D(text="done")
            else:
                yield D(text="VERDICT: APPROVED")
            yield U(prompt_tokens=1, completion_tokens=1)

    with TestClient(create_app(gateway=GW())) as client:
        client.patch("/api/v1/settings", json={
            "story_poll_interval": 0.05, "project_root": repo,
            "write_tools_enabled": True,
            # app-wide verify would never pass; the project's must be used
            "verify_command": "false",
        })
        proj = client.post("/api/v1/projects", json={
            "name": "layout-x", "root": repo,
            "verify_command": "test -f m.py",
        }).json()
        story = client.post("/api/v1/stories", json={
            "title": "marker", "project_id": proj["id"],
        }).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        deadline = time.time() + 30
        while time.time() < deadline:
            cur = client.get(f"/api/v1/stories/{story['id']}").json()
            if cur["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        deadline = time.time() + 30
        while time.time() < deadline:
            cur = client.get(f"/api/v1/stories/{story['id']}").json()
            if cur["status"] in ("human_review", "failed", "done"):
                break
            time.sleep(0.05)
        assert cur["status"] == "human_review", cur.get("error")
        v = cur["verify_result"]
        assert v["passed"] is True and v["command"] == "test -f m.py"


def test_commit_never_lands_in_main_checkout(tmp_path):
    """The 005 incident: if the story worktree is removed mid-run (or its
    registration is stale), commit() must no-op — never stage and commit
    whatever directory the path resolution lands in (an operator's dirty
    main checkout got committed onto main as a 'story' commit)."""
    repo = init_repo(tmp_path)
    # dirty the main checkout the way an operator would
    (Path(repo) / "operator_wip.py").write_text("wip\n")
    # no worktree at all -> commit is a no-op
    assert workspaces.commit(repo, "ghost", "story ghost: coder output") == ""
    # a plain directory (not a registered worktree) at the worktree path
    # must not become a commit target either
    fake = Path(repo) / ".fubala-worktrees" / "ghost"
    fake.mkdir(parents=True)
    (fake / "junk.txt").write_text("x")
    assert workspaces.commit(repo, "ghost", "story ghost: coder output") == ""
    status = subprocess.run(
        ["git", "-C", repo, "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert "operator_wip" in status, "operator WIP must remain uncommitted"
    log = subprocess.run(
        ["git", "-C", repo, "log", "--oneline"], capture_output=True, text=True
    ).stdout
    assert "ghost" not in log, "no story commit may land on the main branch"


def test_run_command_cannot_escape_with_C():
    from app.guardrails import ToolPolicy
    from app.agent_tools import ToolBox

    policy = ToolPolicy(write_tools_enabled=True, permission_mode="strict")
    box = ToolBox(engine=None, project_root="/tmp/zzz-not-a-real-root-x",
                  workspace="/tmp/zzz-not-a-real-root-x",
                  allow_commands=("git",), skip_permissions=True, writable=True)
    denied = box.execute("run_command", json.dumps(
        {"command": "git -C / status --short"}))
    assert "outside the workspace" in denied


def test_cancel_aborts_inflight_coder_run(tmp_path, monkeypatch):
    """POST /cancel while the coder is executing must: kill the pipeline task
    (the code-review gateway call never fires), PRESERVE the worktree and
    branch (a cancelled story's partial work stays inspectable on
    fubala/<story_id>), and land the story in failed with error
    "cancelled by <by>" — after which it is editable and requeueable."""
    import asyncio

    from fastapi.testclient import TestClient

    from app.main import create_app

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "cancel.db"))

    class SlowGateway:
        """Marker-dispatched like GW above (no call-count dispatch — that is
        what made it load-sensitive), but the coder round parks until the
        test cancels it. `parked` (a threading.Event: the app runs on the
        TestClient's event-loop thread, the test body on the pytest thread,
        so an asyncio.Event could never be awaited from here) is set right
        before the park await, so the test knows the round is truly in
        flight. The park only exits on cancellation (CancelledError) or the
        30 s runaway cap — never on a timing guess. `calls` is still counted
        only for the "code review never fired" assertion."""

        def __init__(self):
            self.calls = 0
            self.parked = threading.Event()

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import ToolCall as TC
            from app.providers import Usage as U

            self.calls += 1
            phase = phase_of(messages)
            last = messages[-1].get("content") if messages else None
            if phase == "=== SPEC REQUEST ===":
                yield D(text=MINIMAL_SPEC)
            elif phase == "=== AI REVIEW REQUEST ===":
                yield D(text="fine\n\nVERDICT: APPROVED")
            elif phase == "=== CODE REVIEW REQUEST ===":
                yield D(text="good\n\nVERDICT: APPROVED")  # must never be reached
            elif phase == "=== CODE REQUEST ===":
                if isinstance(last, str) and last.startswith("=== CODE REQUEST ==="):
                    # park mid-flight so we can cancel; signal *before*
                    # awaiting so the cancel can never race ahead of the park
                    self.parked.set()
                    await asyncio.sleep(30)
                    yield TC(
                        id="w1",
                        name="write_file",
                        arguments=json.dumps({"path": "slow.py", "content": "X = 1\n"}),
                    )
                else:
                    self.parked.set()
                    await asyncio.sleep(30)
                    yield D(text="wrote slow.py")
            yield U(prompt_tokens=1, completion_tokens=1)

    gateway = SlowGateway()
    with TestClient(create_app(gateway=gateway)) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "slow"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")

        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")

        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        # wait for the coder to actually be in flight (worktree created)
        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            wt = Path(repo) / ".fubala-worktrees" / story["id"]
            if current["status"] == "executing" and wt.exists():
                break
            time.sleep(0.05)
        assert current["status"] == "executing", current.get("error")

        # the coder round is genuinely parked inside the gateway call now —
        # cancel before this and the pipeline may still be between rounds.
        assert gateway.parked.wait(timeout=30), "coder round never parked"

        res = client.post(f"/api/v1/stories/{story['id']}/cancel", json={"by": "t"})
        assert res.status_code == 200, res.text
        cancelled = res.json()
        assert cancelled["status"] == "failed"
        assert cancelled["error"] == "cancelled by t"
        # the response names the branch the work survives on
        assert cancelled["branch"] == f"fubala/{story['id']}"

        # the worktree is PRESERVED (nothing was written yet, so the wip
        # commit is skipped — but the worktree and branch must survive)
        wt = Path(repo) / ".fubala-worktrees" / story["id"]
        assert wt.exists(), "cancel must keep the worktree for inspection"
        branch = f"fubala/{story['id']}"
        assert (
            subprocess.run(
                ["git", "-C", repo, "rev-parse", "--verify", "--quiet", branch],
                capture_output=True,
            ).returncode
            == 0
        ), "cancel must keep the story branch"
        heads = subprocess.run(
            ["git", "-C", repo, "log", "--oneline", "--first-parent", branch],
            capture_output=True,
            text=True,
        ).stdout
        assert "wip: cancelled" not in heads, (
            "no changes were made: commit() must not create an empty wip commit"
        )

        # the dying task cannot resurrect the story
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(0.1)
        current = client.get(f"/api/v1/stories/{story['id']}").json()
        assert current["status"] == "failed"
        assert current["error"] == "cancelled by t"

        # code review never fired: the pipeline died at the coder stage
        assert gateway.calls <= 4, gateway.calls

        # a cancelled story is editable again...
        assert (
            client.patch(
                f"/api/v1/stories/{story['id']}", json={"title": "renamed"}
            ).status_code
            == 200
        )
        # ...and requeueable
        requeued = client.post(f"/api/v1/stories/{story['id']}/queue")
        assert requeued.status_code == 200
        assert requeued.json()["status"] == "queued"


# ---- take-in-main: the worktree must verify against current main ----------

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


def _takein_wait_status(client, sid, want, timeout=30.0):  # noqa: ANN001
    """Poll GET /stories/{id} until it reaches one of `want` statuses."""
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


def _takein_commit(repo: str, msg: str) -> None:
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", repo,
         "commit", "-qm", msg],
        check=True, capture_output=True,
    )


class TakeInGW:
    """Fake gateway for the stale-base (take-in-main) scenarios.

    Routes by prompt marker (spec / ai review / coder / merge-conflict
    resolution / fix / code review) instead of a call counter, because a
    rejected story's rerun interleaves those phases unpredictably. Every user
    prompt is recorded in `self.prompts` so tests can assert exactly what the
    coder was asked to resolve, and every coder round writes the same file —
    which is what a retried story whose work is already committed does.
    """

    def __init__(
        self,
        write_path: str = "feature.py",
        write_content: str = "X = 1\n",
        rewrite_content: str | None = None,
    ):
        self.write_path = write_path
        self.write_content = write_content
        # what a RERUN's coder round writes into the same file: None keeps
        # every round byte-identical (the trivial case); a different string
        # is what a real retried story does — the worktree then holds an
        # uncommitted change over a file main also touched
        self.rewrite_content = rewrite_content
        self.calls = 0
        self.prompts: list[str] = []

    async def stream_chat(
        self, *, model, messages, tools=None, max_output_tokens=None
    ):
        from app.providers import Delta as D
        from app.providers import ToolCall as TC
        from app.providers import Usage as U

        self.calls += 1
        self.prompts.append(prompt := (messages[-1].get("content") or ""))
        if prompt.startswith("=== CODE REQUEST ==="):
            seen = sum(1 for p in self.prompts if p.startswith("=== CODE REQUEST ==="))
            content = (
                self.write_content
                if seen == 1 or self.rewrite_content is None
                else self.rewrite_content
            )
            yield TC(
                id="w",
                name="write_file",
                arguments=json.dumps(
                    {"path": self.write_path, "content": content}
                ),
            )
        elif prompt.startswith("=== MERGE CONFLICT RESOLUTION REQUEST ==="):
            # a "resolution" that resolves nothing: markers stay in place
            yield D(text="I could not resolve these conflicts")
        elif prompt.startswith("=== FIX REQUEST ==="):
            yield D(text="cannot fix it")
        elif prompt.startswith(("=== CODE REVIEW REQUEST", "=== AI REVIEW REQUEST")):
            yield D(text="VERDICT: APPROVED")
        elif prompt.startswith("=== SPEC REQUEST"):
            yield D(text=MINIMAL_SPEC)
        else:
            yield D(text="ack")
        yield U(prompt_tokens=1, completion_tokens=1)


def _takein_story_to_review_gate(client, repo, title, verify_command):  # noqa: ANN001
    """Queue → spec → human review → approve → first coder round → human
    review. The story returns with its worktree based on the repo's current
    HEAD and its work committed on the story branch."""
    client.patch("/api/v1/settings", json={
        "story_poll_interval": 0.05,
        "project_root": repo,
        "write_tools_enabled": True,
        "verify_command": verify_command,
    })
    sid = client.post("/api/v1/stories", json={"title": title}).json()["id"]
    client.post(f"/api/v1/stories/{sid}/queue")
    cur = _takein_wait_status(client, sid, ("human_review", "failed"))
    assert cur["status"] == "human_review", cur.get("error")
    client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
    cur = _takein_wait_status(client, sid, ("human_review", "failed", "done"))
    assert cur["status"] == "human_review", cur.get("error")
    return sid


def test_take_in_main_verify_sees_merged_main_breaking_change(tmp_path, monkeypatch):
    """AC1: the worktree merges current main before verify runs. Main renames
    a function the story's code calls, so the repo's own test — the verify
    command — must fail in the worktree. A stale base (worktree still sitting
    on main@A) would pass it and ship code that breaks the moment it merges."""
    repo = init_repo(tmp_path)
    (Path(repo) / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _takein_commit(repo, "calc")
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "takein1.db"))

    gw = TakeInGW(
        write_path="caller.py",
        write_content="from calc import add\n\nTOTAL = add(1, 2)\n",
        # the rerun must land a real commit: a byte-identical rewrite is a
        # no-op round under the code-reject gate (AC4), which parks the
        # story before verify ever runs — that is not what this test probes
        rewrite_content="from calc import add\n\nTOTAL = add(1, 2)  # rerun\n",
    )
    with TestClient(create_app(gateway=gw)) as client:
        # verify runs the story's own code, which calls calc.add
        sid = _takein_story_to_review_gate(
            client, repo, "stale base", verify_command="python3 caller.py"
        )

        # main@B renames the function the story's code calls
        (Path(repo) / "calc.py").write_text("def sum_up(a, b):\n    return a + b\n")
        _takein_commit(repo, "rename add -> sum_up")
        client.post(f"/api/v1/stories/{sid}/reject", json={"reason": "rerun"})

        final = _takein_wait_status(client, sid, ("human_review", "failed"))
        assert final["status"] == "human_review", final.get("error")
        assert final["verify_result"]["passed"] is False, (
            "verify ran against a stale base: main's breaking rename was never "
            f"taken into the worktree — verify_result: {final['verify_result']}"
        )


def test_take_in_main_identical_rerun_parks_before_verify(tmp_path, monkeypatch):
    """The no-op gate (AC4) runs before verify: a rerun whose coder round is
    byte-identical to round 1 parks the story, and verify does not run again
    even though main moved (and broke the story's code) in between. On any
    platform where the gate fails open this fails instead of passing silently."""
    # a CI pod does not set this (the image does): verify then writes
    # __pycache__ into the worktree, which must not count as coder work
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    repo = init_repo(tmp_path)
    (Path(repo) / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _takein_commit(repo, "calc")
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "takein1b.db"))

    gw = TakeInGW(
        write_path="caller.py",
        write_content="from calc import add\n\nTOTAL = add(1, 2)\n",
        rewrite_content=None,
    )
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(
            client, repo, "identical rerun", verify_command="python3 caller.py"
        )

        (Path(repo) / "calc.py").write_text("def sum_up(a, b):\n    return a + b\n")
        _takein_commit(repo, "rename add -> sum_up")
        client.post(f"/api/v1/stories/{sid}/reject", json={"reason": "rerun"})

        final = _takein_wait_status(client, sid, ("human_review", "failed"))
        assert final["status"] == "human_review", final.get("error")
        assert final["error"] == "coder made no changes after the human reject", (
            "the no-op gate failed open (reached verify): "
            f"verify_result={final.get('verify_result')} "
            f"base_ref={final.get('base_ref')} "
            f"code_reject_head={final.get('code_reject_head')}"
        )
        # round 1's result: verify did not run again against the merged main
        assert final["verify_result"]["passed"] is True, final["verify_result"]


def test_take_in_main_unrelated_main_file_is_present_and_diff_stays_clean(
    tmp_path, monkeypatch
):
    """AC2: an unrelated main@B file lands in the worktree (verify runs on
    what will actually merge), verify is green, and code_diff carries ONLY
    the story's file — main's own progress never masquerades as coder output."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "takein2.db"))

    gw = TakeInGW(
        write_path="feature.py",
        write_content="X = 1\n",
        # the rerun must land a real change: a byte-identical rewrite is a
        # no-op round under the code-reject gate, which parks the story
        # before verify ever runs (that is AC1's behaviour, not this test's)
        rewrite_content="X = 2  # rerun\n",
    )
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(
            client,
            repo,
            "stale base",
            verify_command="test -f unrelated_from_main.txt",
        )

        (Path(repo) / "unrelated_from_main.txt").write_text("main moved on\n")
        _takein_commit(repo, "unrelated main progress")
        client.post(f"/api/v1/stories/{sid}/reject", json={"reason": "rerun"})

        final = _takein_wait_status(client, sid, ("human_review", "failed"))
        assert final["status"] == "human_review", final.get("error")

        wt = Path(repo) / ".fubala-worktrees" / sid
        assert (wt / "unrelated_from_main.txt").exists(), (
            "the worktree still sits on a stale base — current main was never taken in"
        )
        assert final["verify_result"]["passed"] is True, final["verify_result"]
        diff = final.get("code_diff") or ""
        assert diff.strip(), (
            "code_diff must not be empty — an empty diff reads as "
            "'coder produced no changes' and fails the story"
        )
        assert "feature.py" in diff, "the coder's own work must be the diff"
        assert "unrelated_from_main" not in diff, (
            "main's own progress leaked into the story's code_diff"
        )


class NoOpRetryGW(TakeInGW):
    """Every coder round writes byte-identical content, so a post-reject
    rerun commits nothing: the definition of a silently-ignored no-op."""

    def __init__(self):
        super().__init__(write_path="feature.py", write_content="X = 1\n")


class RecoveringRetryGW(TakeInGW):
    """The first coder round writes the file; the post-reject retry is the
    ONLY round that writes new content, so the rerun lands exactly one new
    commit."""

    def __init__(self):
        super().__init__(
            write_path="feature.py",
            write_content="X = 1\n",
            rewrite_content="X = 2  # addressed the reject\n",
        )


class ReasonedRetryGW(TakeInGW):
    """A retry round that reacts to the human's reasons with real code."""

    def __init__(self):
        super().__init__(write_path="feature.py", write_content="X = 1\n")
        self.retry_writes = 0

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
        from app.providers import Delta as D
        from app.providers import ToolCall as TC
        from app.providers import Usage as U

        prompt = (messages[-1].get("content") or "")
        if prompt.startswith("=== CODE REJECT RETRY ==="):
            self.calls += 1
            self.prompts.append(prompt)
            self.retry_writes += 1
            yield TC(
                id="rw",
                name="write_file",
                arguments=json.dumps(
                    {"path": self.write_path, "content": "X = 3  # retry\n"}
                ),
            )
            yield U(prompt_tokens=1, completion_tokens=1)
            return
        async for event in super().stream_chat(
            model=model,
            messages=messages,
            tools=tools,
            max_output_tokens=max_output_tokens,
        ):
            yield event


def _code_reject_notes(client, sid):
    """note-kind events, parsed, newest last."""
    detail = client.get(f"/api/v1/stories/{sid}").json()
    return [
        json.loads(e["data"])
        for e in detail.get("events") or []
        if e["kind"] == "note"
    ]


def _takein_wait_terminal(client, sid, timeout=40.0):
    """Poll until the story parks somewhere a human must look."""
    deadline = time.time() + timeout
    cur = None
    while time.time() < deadline:
        cur = client.get(f"/api/v1/stories/{sid}").json()
        if cur["status"] in ("human_review", "failed", "done"):
            return cur
        time.sleep(0.05)
    pytest.fail(
        f"never reached a terminal gate; last: {cur and cur['status']} "
        f"{cur and cur.get('error')}"
    )


def test_code_reject_noop_round_is_never_reviewed(tmp_path, monkeypatch):
    """AC1: a human rejects at the code gate; the rerun's coder round
    commits nothing. That no-op round must NOT reach verify or code review
    as if the objection had been addressed: it gets exactly one stronger
    retry whose prompt carries the numbered reason, and when that too
    commits nothing the story parks at the human gate with the explicit
    marker — never requeued into a silent loop."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "crnoop1.db"))

    gw = NoOpRetryGW()
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(
            client, repo, "no-op reject", verify_command=""
        )
        # snapshot what the FIRST (pre-reject) round produced: only work
        # after the reject may be judged
        pre_prompts = len(gw.prompts)
        pre_events = len(client.get(f"/api/v1/stories/{sid}").json()["events"])
        client.post(
            f"/api/v1/stories/{sid}/reject", json={"reason": "needs more logging"}
        )

        final = _takein_wait_terminal(client, sid)
        assert final["status"] == "human_review", final.get("error")
        assert final["error"] == "coder made no changes after the human reject", (
            f"a no-op coder round was not parked with the explicit marker: {final}"
        )

        retries = [
            p for p in gw.prompts[pre_prompts:]
            if p.startswith("=== CODE REJECT RETRY ===")
        ]
        assert len(retries) == 1, (
            f"expected exactly one stronger retry, saw {len(retries)}; "
            f"prompts seen: {[p.splitlines()[0] for p in gw.prompts[pre_prompts:]]}"
        )
        assert "1. needs more logging" in retries[0], (
            f"the retry prompt must carry the numbered human reason: {retries[0]}"
        )
        assert "without a commit is rejected" in retries[0]
        # no verify event, no code-review prompt AFTER the reject: the no-op
        # round was never reviewed as if the objection had been addressed
        events = client.get(f"/api/v1/stories/{sid}").json()["events"][pre_events:]
        assert not any(e["kind"] == "verify" for e in events), (
            "verify ran for a no-op post-reject round"
        )
        review_prompts = [
            p for p in gw.prompts[pre_prompts:]
            if p.startswith("=== CODE REVIEW REQUEST")
        ]
        assert not review_prompts, (
            f"a no-op round reached code review: {review_prompts}"
        )


def test_code_reject_retry_that_commits_is_reviewed_normally(tmp_path, monkeypatch):
    """AC2: the rerun reacts to the reject with a real commit. Verify and
    code review then run exactly as they do today, no retry round fires,
    and the marker is dropped so later reruns are not re-checked."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "crnoop2.db"))

    gw = RecoveringRetryGW()
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(
            client, repo, "reacting reject", verify_command="test -f feature.py"
        )
        pre_prompts = len(gw.prompts)
        pre_events = len(client.get(f"/api/v1/stories/{sid}").json()["events"])
        client.post(
            f"/api/v1/stories/{sid}/reject", json={"reason": "needs more logging"}
        )

        final = _takein_wait_terminal(client, sid)
        assert final["status"] == "human_review", final.get("error")
        # verify ran (the marker never short-circuited a real round)
        events = client.get(f"/api/v1/stories/{sid}").json()["events"][pre_events:]
        assert any(e["kind"] == "verify" for e in events), (
            "verify never ran for a post-reject round that committed"
        )
        review_prompts = [
            p for p in gw.prompts[pre_prompts:]
            if p.startswith("=== CODE REVIEW REQUEST")
        ]
        assert review_prompts, "code review never ran for a committed round"
        # no retry round fired: the first coder round already committed
        assert not any(
            p.startswith("=== CODE REJECT RETRY ===") for p in gw.prompts
        ), "a retry fired although the rerun already committed"


def test_code_review_must_address_each_human_reject_reason(tmp_path, monkeypatch):
    """AC3: with code-gate reject reasons on record, the review prompt
    carries them plus the per-point reply contract. A reviewer who approves
    but leaves a reason explicitly NOT ADDRESSED is overridden to rejected
    and the story requeued — the human's objection cannot be rubber-stamped
    away."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "crnoop3.db"))

    class ReviewGW(ReasonedRetryGW):
        def __init__(self):
            super().__init__()
            self.reasoned_reviews = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import Usage as U

            prompt = (messages[-1].get("content") or "")
            if prompt.startswith("=== CODE REVIEW REQUEST"):
                self.calls += 1
                self.prompts.append(prompt)
                # key on the prompt, not a call counter: the FIRST round's
                # code review runs before any reject exists, and counting it
                # would spend the "NOT ADDRESSED" reply on a review that has
                # no reasons to address
                if "## Human reject reasons to verify" in prompt:
                    self.reasoned_reviews += 1
                    if self.reasoned_reviews == 1:
                        # approving verdict with one reason explicitly left
                        # unaddressed: the human's objection must win
                        yield D(
                            text="Reason 1: NOT ADDRESSED — no logging was added\n\n"
                            "VERDICT: APPROVED"
                        )
                    else:
                        # later rounds address everything: no infinite loop
                        yield D(
                            text="Reason 1: ADDRESSED\n\nVERDICT: APPROVED"
                        )
                else:
                    # no reasons on record (pre-reject review): plain verdict
                    yield D(text="VERDICT: APPROVED")
                yield U(prompt_tokens=1, completion_tokens=1)
                return
            async for event in super().stream_chat(
                model=model,
                messages=messages,
                tools=tools,
                max_output_tokens=max_output_tokens,
            ):
                yield event

    gw = ReviewGW()
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(
            client, repo, "review reasons", verify_command=""
        )
        pre_prompts = len(gw.prompts)
        pre_events = len(client.get(f"/api/v1/stories/{sid}").json()["events"])
        client.post(
            f"/api/v1/stories/{sid}/reject",
            json={"reason": "needs more logging and a docstring"},
        )

        # the reviewer approved over an unaddressed reason: the story must be
        # requeued as AI-rejected. It then reruns and (this gateway now
        # addresses the reason) reaches the human gate — so assert on the
        # durable event history rather than racing the transient status.
        final = _takein_wait_terminal(client, sid, timeout=60)
        assert final["status"] == "human_review", final.get("error")
        detail = client.get(f"/api/v1/stories/{sid}").json()
        after = detail["events"][pre_events:]
        ai_verdicts = [
            json.loads(e["data"])["verdict"]
            for e in after
            if e["kind"] == "ai_review"
        ]
        assert "rejected" in ai_verdicts, (
            f"an approving verdict over an unaddressed reason was not "
            f"overridden to rejected: {ai_verdicts}"
        )
        requeued = [
            json.loads(e["data"])
            for e in after
            if e["kind"] == "status" and json.loads(e["data"]).get("to") == "queued"
        ]
        assert any(r.get("from") == "ai_review" for r in requeued), (
            f"the story was never requeued from ai_review: {requeued}"
        )
        assert final["status"] != "failed"

        review_prompts = [
            p for p in gw.prompts[pre_prompts:]
            if p.startswith("=== CODE REVIEW REQUEST")
        ]
        assert review_prompts, "code review never ran"
        assert "## Human reject reasons to verify" in review_prompts[0]
        assert "1. needs more logging and a docstring" in review_prompts[0]
        assert "NOT ADDRESSED" in review_prompts[0]
        assert "Reason N: ADDRESSED" in review_prompts[0]


def test_respec_clears_the_code_reject_marker(tmp_path, monkeypatch):
    """Step 6b hygiene: a re-spec restarts execution from scratch, so the
    code-gate reject marker must not survive into the next round — otherwise
    a story re-specced after a human reject would park with "coder made no
    changes" even though the branch was discarded and re-forked."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "crnoop5.db"))

    gw = NoOpRetryGW()
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(
            client, repo, "re-spec marker", verify_command=""
        )
        client.post(
            f"/api/v1/stories/{sid}/reject", json={"reason": "needs more logging"}
        )
        # the no-op rerun parks at the human gate with the marker recorded
        parked = _takein_wait_terminal(client, sid)
        assert parked["status"] == "human_review", parked.get("error")
        assert parked["error"] == "coder made no changes after the human reject"

        # the operator re-specs the parked story: the marker must be gone
        # (/re-spec is guarded to failed/backlog/queued, like the spec-side
        # hygiene tests, which poke the status directly). "backlog", not
        # "queued": a queued story is a scheduler candidate, so the runner
        # could claim it into executing before the re-spec lands and the
        # 409 would turn this test into a race.
        from sqlmodel import Session

        from app.models import Story

        with Session(client.app.state.engine) as db:
            row = db.get(Story, sid)
            row.status = "backlog"
            db.add(row)
            db.commit()
        resp = client.post(f"/api/v1/stories/{sid}/re-spec", json={})
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["status"] == "queued", out
        # the marker is internal bookkeeping on the story row: assert it is
        # gone so a fresh round is never judged against the discarded branch
        with Session(client.app.state.engine) as db:
            row = db.get(Story, sid)
            assert row.code_reject_head is None, (
                "the code-gate reject marker survived the re-spec: "
                f"{row.code_reject_head}"
            )

        # the fresh round must NOT be judged against the discarded branch:
        # NoOpRetryGW writes the same content into a rebuilt worktree, which
        # is a brand-new commit on a brand-new branch — anything but a no-op
        final = _takein_wait_terminal(client, sid, timeout=60)
        assert final["status"] == "human_review", final.get("error")
        assert final.get("error") != "coder made no changes after the human reject", (
            "the code-gate reject marker survived the re-spec and parked a "
            f"fresh round: {final}"
        )


def test_code_reject_noop_when_only_the_takein_merge_landed(tmp_path, monkeypatch):
    """AC4: main advances between the reject and the rerun. The take-in
    merge commit lands on the story branch but no coder commit does, so
    the round still counts as a no-op: merge commits are not agent work."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "crnoop4.db"))

    gw = NoOpRetryGW()
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(
            client, repo, "merge only", verify_command=""
        )
        # main moves AFTER the reject pinned the branch head
        (Path(repo) / "main_moved.txt").write_text("moved\n")
        _takein_commit(repo, "main advances")
        client.post(
            f"/api/v1/stories/{sid}/reject", json={"reason": "needs more logging"}
        )

        final = _takein_wait_terminal(client, sid)
        assert final["status"] == "human_review", final.get("error")
        assert final["error"] == "coder made no changes after the human reject", (
            f"the take-in merge was counted as coder work: {final}"
        )
        # the merge really did land on the branch: main's file is in the tree
        wt = Path(repo) / ".fubala-worktrees" / sid
        assert (wt / "main_moved.txt").exists(), (
            "main never advanced into the worktree — the fixture is not "
            "exercising the take-in path"
        )


def test_take_in_main_conflict_gets_one_resolution_round_then_marked(
    tmp_path, monkeypatch
):
    """AC3: main@B edits the file the coder wrote. Exactly one coder round is
    spent resolving (its prompt names the conflicted path); markers left
    behind park the story in human review with the conflicts named — and the
    approve endpoint refuses to merge it, so markers never land on main."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "takein3.db"))

    gw = TakeInGW(write_path="feature.py", write_content="X = 1\n")
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(client, repo, "stale base", verify_command="")

        # main@B edits the very file the story's coder wrote
        (Path(repo) / "feature.py").write_text("X = 2  # changed on main\n")
        _takein_commit(repo, "main edits feature.py")
        client.post(f"/api/v1/stories/{sid}/reject", json={"reason": "rerun"})

        final = _takein_wait_status(client, sid, ("human_review", "failed"))
        assert final["status"] == "human_review", final.get("error")
        assert "merge with main has conflicts" in (final.get("error") or ""), (
            final.get("error")
        )

        resolutions = [
            p
            for p in gw.prompts
            if p.startswith("=== MERGE CONFLICT RESOLUTION REQUEST ===")
        ]
        assert len(resolutions) == 1, (
            f"expected exactly one coder resolution round, saw {len(resolutions)}; "
            f"prompts seen: {[p.splitlines()[0] for p in gw.prompts]}"
        )
        assert "feature.py" in resolutions[0]

        # never silently merged: approve is refused, not merged
        res = client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
        assert res.status_code == 409, res.text
        assert "conflict" in res.json()["detail"].lower()


def test_take_in_main_commits_coder_output_before_merging(tmp_path, monkeypatch):
    """The coder commit runs BEFORE the take-in merge. With it the other way
    round, an uncommitted coder change touching a file main also moved makes
    git refuse the merge ("Your local changes would be overwritten") — that
    surfaces as WorkspaceError and the story fails outright, with no coder
    resolution round at all. Committed, the same overlap is a normal merge
    conflict the coder gets exactly one round on (spec step 4 ordering)."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "takein4.db"))

    gw = TakeInGW(
        write_path="feature.py",
        write_content="X = 1\n",
        # the rerun writes DIFFERENT content than the committed attempt, so
        # the worktree holds a real uncommitted change over feature.py
        rewrite_content="X = 2  # second attempt\n",
    )
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(client, repo, "stale base", verify_command="")

        # main@B edits the same file the rerun's coder output touches
        (Path(repo) / "feature.py").write_text("X = 9  # changed on main\n")
        _takein_commit(repo, "main edits feature.py")
        client.post(f"/api/v1/stories/{sid}/reject", json={"reason": "rerun"})

        final = _takein_wait_status(client, sid, ("human_review", "failed"))
        assert final["status"] == "human_review", final.get("error")
        # a hard git refusal would have failed the story; a conflict the
        # coder could not resolve parks it in human review with the paths
        assert "merge with main has conflicts" in (final.get("error") or ""), (
            final.get("error")
        )
        assert "overwritten by merge" not in (final.get("error") or ""), (
            "git refused the merge over uncommitted coder output — the coder "
            f"commit must run before the take-in: {final.get('error')}"
        )
        resolutions = [
            p
            for p in gw.prompts
            if p.startswith("=== MERGE CONFLICT RESOLUTION REQUEST ===")
        ]
        assert len(resolutions) == 1, (
            f"expected exactly one coder resolution round, saw {len(resolutions)}; "
            f"prompts seen: {[p.splitlines()[0] for p in gw.prompts]}"
        )
        assert "feature.py" in resolutions[0]


def test_take_in_main_uses_project_root_head_not_a_branch_named_main(tmp_path):
    """Coordinator gate: "current main" is the PROJECT ROOT's HEAD commit —
    whatever branch it sits on (`integration`, `fubala/integration`, ...),
    resolved with `git rev-parse HEAD`, never a branch literally named "main".

    And the diff base is the merge-base computed in the project root: after
    the take-in, main's own progress is excluded while the story's work stays
    in the diff (non-empty — an empty diff reads as "coder produced no
    changes" and fails the story)."""
    repo = init_repo(tmp_path)
    # the project root moves onto a branch with a different name
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", repo,
         "checkout", "-qb", "integration"],
        check=True, capture_output=True,
    )

    wt, base, created = workspaces.create(repo, "integ1")
    assert created is True
    (wt / "story_work.py").write_text("VALUE = 42\n")
    workspaces.commit(repo, "integ1", "story integ1: coder output")

    # main advances ON BRANCH integration (an unrelated file, no conflict)
    (Path(repo) / "from_integration_branch.txt").write_text("moved on\n")
    _takein_commit(repo, "integration branch progress")

    conflicted, paths = workspaces.take_in_main(repo, "integ1")
    assert conflicted is False, paths
    assert (wt / "from_integration_branch.txt").exists(), (
        "the worktree took in a branch named 'main' instead of the project "
        "root's HEAD (integration)"
    )

    workspaces.commit(repo, "integ1", "story integ1: merge main")
    diff = workspaces.diff(repo, "integ1", base)
    assert diff.strip(), "diff collapsed to empty — the base rewound past the pin"
    assert "story_work.py" in diff, "the coder's own work must be the diff"
    assert "from_integration_branch" not in diff, (
        "main's own progress leaked into the story's code_diff"
    )


def test_markers_in_untouched_main_file_do_not_block_merge(tmp_path):
    """AC1: conflict-marker detection only scans files the STORY changed
    (plus what the take-in reported conflicted). A literal `<<<<<<< HEAD`
    line in a main file the story never touched is pre-existing project
    content — it must not block the approve/merge."""
    repo = init_repo(tmp_path)
    (Path(repo) / "conflict_fixture.txt").write_text(
        "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> other\n"
    )
    _takein_commit(repo, "main carries a conflict fixture")

    wt, _base, _new = workspaces.create(repo, "cm1")
    (wt / "story.py").write_text("VALUE = 1\n")
    workspaces.commit(repo, "cm1", "story cm1: unrelated work")

    out = workspaces.merge(repo, "cm1")  # must NOT raise
    assert "already merged" not in out, out
    assert (Path(repo) / "story.py").exists(), "the story's file must land on main"
    assert (Path(repo) / "conflict_fixture.txt").exists(), (
        "the untouched fixture stays exactly as main had it"
    )
    assert "<<<<<<< HEAD" in (Path(repo) / "conflict_fixture.txt").read_text()


def test_markers_in_story_touched_file_block_merge(tmp_path):
    """AC2: a `<<<<<<< HEAD` left in a file the story TOUCHED still blocks
    the merge — whether the edit is uncommitted (a) or already `git add`-ed
    (b). Sub-case (b) is the regression guard: `git add` would stage the
    markers as a resolution, so marker scanning must look past the index."""
    for mode in ("loose", "staged", "committed"):
        base = tmp_path / mode
        base.mkdir(parents=True, exist_ok=True)
        repo = init_repo(base)
        wt, _base, _new = workspaces.create(repo, "cm2")
        # a file that exists on main and is edited by the story
        (wt / "story.py").write_text("VALUE = 0\n")
        workspaces.commit(repo, "cm2", "story cm2: touch the file")
        # the coder leaves unresolved markers in that touched file
        (wt / "story.py").write_text(
            "<<<<<<< HEAD\nVALUE = 1\n=======\nVALUE = 2\n>>>>>>> main\n"
        )
        if mode == "staged":
            subprocess.run(
                ["git", "-C", str(wt), "add", "story.py"],
                check=True, capture_output=True,
            )
        elif mode == "committed":
            # markers committed onto the story branch BEFORE any take-in must
            # still block: the touched set is computed vs the merge-base, not
            # the worktree HEAD
            workspaces.commit(repo, "cm2", "story cm2: markers committed")
        with pytest.raises(workspaces.WorkspaceError, match="merge with main has conflicts") as ei:
            workspaces.merge(repo, "cm2")
        assert "story.py" in str(ei.value), (
            f"the conflicted path must be named (mode={mode}): {ei.value}"
        )
        assert not (Path(repo) / "story.py").exists(), (
            f"nothing must land on main (mode={mode})"
        )


def test_cancel_during_slow_take_in_main(tmp_path, monkeypatch):
    """POST /cancel while a slow git thread (take_in_main) is in flight must
    still drop the worktree: a merge that lands after the cancel must not
    resurrect the story's scratch checkout."""

    real = workspaces.take_in_main

    def slow(project_root, story_id):
        time.sleep(1.5)  # a slow merge / loaded machine
        return real(project_root, story_id)

    monkeypatch.setattr(workspaces, "take_in_main", slow)

    class SlowGateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D, ToolCall as TC, Usage as U

            self.calls += 1
            if self.calls == 1:
                yield D(text=MINIMAL_SPEC)
            elif self.calls == 2:
                yield D(text="fine\n\nVERDICT: APPROVED")
            elif self.calls == 3:
                yield TC(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps({"path": "slow.py", "content": "X = 1\n"}),
                )
            yield U(prompt_tokens=1, completion_tokens=1)

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "cancel2.db"))
    with TestClient(create_app(gateway=SlowGateway())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "slow"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")

        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")

        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            wt = Path(repo) / ".fubala-worktrees" / story["id"]
            if current["status"] == "executing" and wt.exists():
                break
            time.sleep(0.05)
        assert current["status"] == "executing", current.get("error")

        # let the coder round finish so the pipeline enters the slow
        # take_in_main thread, then cancel
        time.sleep(0.6)
        res = client.post(f"/api/v1/stories/{story['id']}/cancel", json={"by": "t"})
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "failed"
        assert res.json()["error"] == "cancelled by t"

        # the worktree is PRESERVED: whether the wip commit lands before the
        # merge thread resumes or the other way round, the checkout must
        # still exist afterwards (both orderings are safe — _git retries
        # index.lock; a merge landing after the wip commit simply carries
        # it forward)
        assert (Path(repo) / ".fubala-worktrees" / story["id"]).exists(), (
            "worktree was removed by cancel"
        )
        branch = f"fubala/{story['id']}"
        assert (
            subprocess.run(
                ["git", "-C", repo, "rev-parse", "--verify", "--quiet", branch],
                capture_output=True,
            ).returncode
            == 0
        ), "branch was deleted by cancel"


# ---- cancel preserves worktree/branch (wip: cancelled) --------------------


def test_cancel_commits_wip_and_names_branch(tmp_path, monkeypatch):
    """Cancel executing with UNCOMMITTED coder changes: the worktree survives,
    its changes are committed as 'wip: cancelled' on fubala/<id>, the story
    lands failed with error 'cancelled by t', and the cancel response carries
    branch='fubala/<id>' so an operator can push/fetch the partial work."""
    import asyncio

    from app.main import create_app

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "wip.db"))

    class ParkedCoderGateway:
        """Spec+AI-review approved fast; the coder round parks until the test
        cancels, having already written an untracked file into the worktree
        (the uncommitted state a real cancel would interrupt mid-write)."""

        def __init__(self):
            self.parked = threading.Event()
            self.code_rounds = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import ToolCall as TC
            from app.providers import Usage as U

            phase = phase_of(messages)
            if phase == "=== SPEC REQUEST ===":
                yield D(text=MINIMAL_SPEC)
            elif phase == "=== AI REVIEW REQUEST ===":
                yield D(text="fine\n\nVERDICT: APPROVED")
            elif phase == "=== CODE REQUEST ===":
                self.code_rounds += 1
                if self.code_rounds == 1:
                    # round 1 ends with the tool call; run_agent executes it
                    # BETWEEN rounds, so partial.py exists on disk by the time
                    # round 2 starts
                    yield TC(
                        id="w1",
                        name="write_file",
                        arguments=json.dumps(
                            {"path": "partial.py", "content": "PARTIAL = 1\n"}
                        ),
                    )
                else:
                    # round 2: the work is on disk and uncommitted — park
                    # mid-run so the test can cancel the executing story
                    self.parked.set()
                    await asyncio.sleep(30)
                    yield D(text="done")
            yield U(prompt_tokens=1, completion_tokens=1)

    gateway = ParkedCoderGateway()
    with TestClient(create_app(gateway=gateway)) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "wip"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})

        wt = Path(repo) / ".fubala-worktrees" / story["id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] == "executing" and wt.exists():
                break
            time.sleep(0.05)
        assert current["status"] == "executing", current.get("error")

        # the coder round is parked AFTER the write_file tool call landed, so
        # the worktree provably holds the uncommitted change we cancel on
        assert gateway.parked.wait(timeout=30), "coder round never parked"
        # parked.set() fires when the gateway YIELDS the tool call — the
        # runner may still be a tick behind executing it. Cancel only once
        # the file is really on disk or the wip commit races an empty tree.
        partial = wt / "partial.py"
        deadline = time.time() + 30
        while not partial.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert partial.exists(), "write_file never landed in the worktree"

        res = client.post(f"/api/v1/stories/{story['id']}/cancel", json={"by": "t"})
        assert res.status_code == 200, res.text
        cancelled = res.json()
        assert cancelled["status"] == "failed"
        assert cancelled["error"] == "cancelled by t"
        assert cancelled["branch"] == f"fubala/{story['id']}"

        # worktree + branch survive, and the branch head IS the wip commit
        assert wt.exists(), "cancel must keep the worktree"
        branch = f"fubala/{story['id']}"
        head = subprocess.run(
            ["git", "-C", repo, "log", "-1", "--format=%s", branch],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head == "wip: cancelled", head
        # the partial coder change is IN that commit
        show = subprocess.run(
            ["git", "-C", repo, "show", "--name-only", "--format=", branch],
            capture_output=True,
            text=True,
        ).stdout
        assert "partial.py" in show, show
        # the worktree checkout reflects the committed state (clean tree)
        status = subprocess.run(
            ["git", "-C", wt, "status", "--porcelain"], capture_output=True, text=True
        ).stdout.strip()
        assert not status, f"changes were committed, worktree should be clean: {status}"

        # /discard is now the removal path: worktree and branch are gone,
        # the story returns to backlog
        d = client.post(f"/api/v1/stories/{story['id']}/discard")
        assert d.status_code == 200, d.text
        discarded = d.json()
        assert discarded["status"] == "backlog"
        assert discarded["base_ref"] is None
        assert not wt.exists(), "discard must remove the worktree"
        assert (
            subprocess.run(
                ["git", "-C", repo, "rev-parse", "--verify", "--quiet", branch],
                capture_output=True,
            ).returncode
            != 0
        ), "discard must delete the story branch"


def test_cancel_with_unresolved_conflicts_skips_wip_and_notes(tmp_path, monkeypatch):
    """A conflicted worktree must never get a wip commit: `git add -A` would
    stage the markers as if they were a resolution. Cancel instead records a
    note event naming the conflicted paths, and keeps everything else."""
    import asyncio

    from app.main import create_app

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "conflict.db"))

    class ParkedCoderGateway:
        """Writes a file that carries conflict markers (what a half-done
        resolution round leaves behind) and parks so the test can cancel."""

        def __init__(self):
            self.parked = threading.Event()
            self.code_rounds = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import ToolCall as TC
            from app.providers import Usage as U

            phase = phase_of(messages)
            if phase == "=== SPEC REQUEST ===":
                yield D(text=MINIMAL_SPEC)
            elif phase == "=== AI REVIEW REQUEST ===":
                yield D(text="fine\n\nVERDICT: APPROVED")
            elif phase == "=== CODE REQUEST ===":
                self.code_rounds += 1
                if self.code_rounds == 1:
                    # round 1 ends with the tool call; run_agent executes it
                    # BETWEEN rounds, so story.py exists on disk before the
                    # round that parks
                    yield TC(
                        id="w1",
                        name="write_file",
                        arguments=json.dumps(
                            {
                                "path": "story.py",
                                "content": (
                                    "<<<<<<< HEAD\nVALUE = 1\n"
                                    "=======\nVALUE = 2\n>>>>>>> main\n"
                                ),
                            }
                        ),
                    )
                else:
                    # round 2: the markered file is on disk and uncommitted —
                    # park mid-run so the test can cancel the executing story
                    self.parked.set()
                    await asyncio.sleep(30)
                    yield D(text="done")
            yield U(prompt_tokens=1, completion_tokens=1)

    gateway = ParkedCoderGateway()
    with TestClient(create_app(gateway=gateway)) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "conflicted"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})

        wt = Path(repo) / ".fubala-worktrees" / story["id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{story['id']}").json()
            if current["status"] == "executing" and wt.exists():
                break
            time.sleep(0.05)
        # the coder round parks after writing the conflict-marker file;
        # the markered file is a story-touched path carrying markers, so
        # unmerged() will report it and NO commit may be created. Wait for
        # the file to really be on disk (parked.set() fires at yield time)
        # or cancel races ahead of the write.
        assert gateway.parked.wait(timeout=30), "coder round never parked"
        story_file = wt / "story.py"
        deadline = time.time() + 30
        while not story_file.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert story_file.exists(), "write_file never landed in the worktree"

        res = client.post(f"/api/v1/stories/{story['id']}/cancel", json={"by": "t"})
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "failed"

        # the markered file is a story-touched path carrying markers, so
        # unmerged() reports it and NO commit may be created
        branch = f"fubala/{story['id']}"
        tip = subprocess.run(
            ["git", "-C", repo, "rev-parse", branch], capture_output=True, text=True
        ).stdout.strip()
        # base_ref is pinned in the coder's start transaction, which may
        # commit after the worktree directory appears: read it AFTER the
        # cancel, never from the snapshot the wait loop above took
        base_ref = client.get(f"/api/v1/stories/{story['id']}").json()["base_ref"]
        assert base_ref, "the coder run never pinned its base_ref"
        assert tip == base_ref, (
            f"wip commit was created despite unresolved conflicts "
            f"({tip} != base {base_ref})"
        )
        # the file keeps its markers on disk (nothing was staged away)
        assert "<<<<<<< HEAD" in (wt / "story.py").read_text()

        # the skip is recorded as a note event naming the path
        events = client.get(f"/api/v1/stories/{story['id']}").json()["events"]
        notes = [
            json.loads(e["data"])
            for e in events
            if e["kind"] == "note" and "wip" in e["data"]
        ]
        assert notes, f"no 'wip skipped' note among events: {events}"
        assert any(
            "conflicts" in str(n.get("wip", "")) and "story.py" in str(n.get("paths", []))
            for n in notes
        ), notes


def test_discard_from_human_review_unchanged(tmp_path, monkeypatch):
    """The widened /discard must not weaken the original gate: human_review +
    base_ref still discards (worktree and branch removed, story to backlog),
    and the same call still 409s for a story without executed work."""
    from app.main import create_app

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "hr.db"))

    class GW:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import ToolCall as TC
            from app.providers import Usage as U

            self.calls += 1
            if self.calls == 1:
                yield D(text=MINIMAL_SPEC)
            elif self.calls == 2:
                yield D(text="fine\n\nVERDICT: APPROVED")
            elif self.calls == 3:
                yield TC(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps({"path": "m.py", "content": "m = 1\n"}),
                )
            elif self.calls == 4:
                yield D(text="done")
            else:
                yield D(text="fine\n\nVERDICT: APPROVED")
            yield U(prompt_tokens=1, completion_tokens=1)

    with TestClient(create_app(gateway=GW())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
            },
        )
        sid = client.post("/api/v1/stories", json={"title": "gate"}).json()["id"]
        client.post(f"/api/v1/stories/{sid}/queue")
        # first the SPEC gate: base_ref is still None here
        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{sid}").json()
            if current["status"] in ("human_review", "failed"):
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")
        assert current["base_ref"] is None, "no coder work yet — nothing pinned"

        # drive the coder round: write m.py, done, code review approved ->
        # the story re-enters human_review WITH executed work pinned
        client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
        wt = Path(repo) / ".fubala-worktrees" / sid
        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(f"/api/v1/stories/{sid}").json()
            if current["status"] == "human_review" and current["base_ref"]:
                break
            time.sleep(0.05)
        assert current["status"] == "human_review", current.get("error")
        assert current["base_ref"], "coder work must pin base_ref at the gate"
        assert wt.exists(), "executed work must have a worktree"
        assert current["branch"] == f"fubala/{sid}", (
            "the branch carrying executed work is surfaced on the story"
        )

        # discard from the human gate: exactly the pre-existing behaviour
        d = client.post(f"/api/v1/stories/{sid}/discard")
        assert d.status_code == 200, d.text
        discarded = d.json()
        assert discarded["status"] == "backlog"
        assert discarded["base_ref"] is None
        assert discarded["branch"] is None, "no base_ref -> no branch advertised"
        assert not (Path(repo) / ".fubala-worktrees" / sid).exists()
        assert (
            subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"fubala/{sid}",
                ],
                capture_output=True,
            ).returncode
            != 0
        ), "discard from human_review must delete the branch"

        # and the guard still holds for a story with nothing executed: a
        # fresh backlog story without base_ref cannot be discarded
        other = client.post("/api/v1/stories", json={"title": "fresh"}).json()
        guard = client.post(f"/api/v1/stories/{other['id']}/discard")
        assert guard.status_code == 409, guard.text
