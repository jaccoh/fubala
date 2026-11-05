"""Stories bound to a project must be reviewed and merged against the
PROJECT'S root, not the global `project_root` setting.

Regressions covered (both were red before the fix):
- `_run_code_review` diffed the global root, so a project story with real
  work looked like "no changes" and eventually failed.
- approve in merge mode merged/discarded against the global root, so a
  project story's approve 409'd ("merge failed") instead of landing in the
  project's own checkout.

Helpers are duplicated from test_delivery_push.py: the tests directory has
no importable package (tests are collected as top-level modules)."""

import json
import subprocess
import time
from pathlib import Path

import pytest
from conftest import MINIMAL_SPEC
from fastapi.testclient import TestClient

from app.main import create_app


def init_repo(tmp_path, name: str) -> str:
    repo = tmp_path / name
    repo.mkdir()
    (repo / "README.md").write_text(f"# {name}\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return str(repo)


def _git_out(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=True
    ).stdout.strip()


class ScriptedGW:
    """spec -> ai review approve -> coder write -> code review approve."""

    def __init__(self):
        self.calls = 0

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
        from app.providers import Delta as D
        from app.providers import ToolCall as TC
        from app.providers import Usage as U

        self.calls += 1
        if self.calls == 1:  # spec
            yield D(text=MINIMAL_SPEC)
        elif self.calls == 2:  # ai review of spec
            yield D(text="fine\n\nVERDICT: APPROVED")
        elif self.calls == 3:  # coder: one write
            yield TC(
                id="w1",
                name="write_file",
                arguments=json.dumps(
                    {"path": "feature.py", "content": "VALUE = 'projected'\n"}
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


def note_events(detail: dict) -> list[dict]:
    return [
        json.loads(e.get("data") or "{}")
        for e in detail.get("events", [])
        if e.get("kind") == "note"
    ]


def _project_story(client, repo_a: str, repo_b: str) -> str:
    """Global root = A, a project rooted at B, and a queued story in it."""
    client.patch(
        "/api/v1/settings",
        json={
            "story_poll_interval": 0.05,
            "project_root": repo_a,
            "write_tools_enabled": True,
        },
    )
    project = client.post("/api/v1/projects", json={"name": "p", "root": repo_b}).json()
    assert project["delivery"] == "merge"
    story = client.post(
        "/api/v1/stories",
        json={"title": "feature", "project_id": project["id"]},
    ).json()
    return story["id"]


def _run_to_code_review_gate(client, sid: str) -> dict:
    """Queue, pass the spec's human gate, and run the coder + code review.

    Returns the story once it is back at human_review (the post-code gate)
    or failed — the two outcomes the pipeline can produce."""
    client.post(f"/api/v1/stories/{sid}/queue")
    spec_gate = wait_status(client, sid, ("human_review", "failed"))
    assert spec_gate["status"] == "human_review", spec_gate.get("error")
    # approving the spec hands the story to the coder; the next stop is the
    # code-review human gate (or a failure anywhere in between)
    client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
    return wait_status(client, sid, ("human_review", "failed"))


def test_code_review_uses_the_story_project_root(tmp_path, monkeypatch):
    """AC1: a project story with real work reaches human_review.

    Pre-fix this failed with "coder produced no changes after retries" —
    the review diffed the GLOBAL root (repo A), where the story's worktree
    and branch do not exist."""
    repo_a = init_repo(tmp_path, "a")
    repo_b = init_repo(tmp_path, "b")
    head_a = _git_out(repo_a, "rev-parse", "HEAD")
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "roots.db"))

    with TestClient(create_app(gateway=ScriptedGW())) as client:
        sid = _project_story(client, repo_a, repo_b)
        current = _run_to_code_review_gate(client, sid)
        assert current["status"] == "human_review", current.get("error")

        # the "no changes" requeue note must never have been recorded
        detail = client.get(f"/api/v1/stories/{sid}").json()
        assert not [n for n in note_events(detail) if "no changes" in n.get("reason", "")]

        # the work really did land in B's worktree — the review approved
        # actual changes, not an empty diff read from the wrong root
        assert (Path(repo_b) / ".fubala-worktrees" / sid / "feature.py").exists()
        # and A stayed untouched the whole time
        assert _git_out(repo_a, "rev-parse", "HEAD") == head_a
        assert not (Path(repo_a) / ".fubala-worktrees" / sid).exists()


def test_story_detail_diffs_the_story_project_root(tmp_path, monkeypatch):
    """AC3: the detail endpoint shows the work done in the project's repo.

    Pre-fix, GET /stories/{id} diffed the GLOBAL root (repo A) where the
    story's worktree does not exist, so WorkspaceError swallowed the diff
    and code_diff came back None (empty panel diff). /diff already had the
    override and is asserted here as the control."""
    repo_a = init_repo(tmp_path, "a")
    repo_b = init_repo(tmp_path, "b")
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "detail.db"))

    with TestClient(create_app(gateway=ScriptedGW())) as client:
        sid = _project_story(client, repo_a, repo_b)
        current = _run_to_code_review_gate(client, sid)
        assert current["status"] == "human_review", current.get("error")

        detail = client.get(f"/api/v1/stories/{sid}").json()
        assert detail["code_diff"], "detail code_diff must not be empty for a project story"
        assert "feature.py" in detail["code_diff"]

        # control: the raw diff endpoint resolves the same root
        raw = client.get(f"/api/v1/stories/{sid}/diff")
        assert raw.status_code == 200, raw.text
        assert "feature.py" in raw.text


def test_approve_merges_a_project_story_into_its_own_repo(tmp_path, monkeypatch):
    """AC2: merge-mode approve lands the work in the project's checkout.

    Pre-fix this 409'd with "merge failed" — merge/discard ran against the
    GLOBAL root, which has no fubala/<sid> branch."""
    repo_a = init_repo(tmp_path, "a")
    repo_b = init_repo(tmp_path, "b")
    head_a = _git_out(repo_a, "rev-parse", "HEAD")
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "approve.db"))

    with TestClient(create_app(gateway=ScriptedGW())) as client:
        sid = _project_story(client, repo_a, repo_b)
        current = _run_to_code_review_gate(client, sid)
        assert current["status"] == "human_review", current.get("error")

        res = client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "done"

        # the work is on B's main checkout, and its worktree is gone
        assert (Path(repo_b) / "feature.py").read_text() == "VALUE = 'projected'\n"
        assert not (Path(repo_b) / ".fubala-worktrees" / sid).exists()
        assert "VALUE = 'projected'" in _git_out(repo_b, "show", "HEAD:feature.py")

        # the global root A never saw the story
        assert _git_out(repo_a, "rev-parse", "HEAD") == head_a
        assert not (Path(repo_a) / "feature.py").exists()
        assert not (Path(repo_a) / ".fubala-worktrees" / sid).exists()

        status_events = [
            json.loads(e.get("data") or "{}")
            for e in client.get(f"/api/v1/stories/{sid}").json().get("events", [])
            if e.get("kind") == "status"
        ]
        assert status_events[-1].get("merged") is True


def test_post_story_without_project_id_is_refused_with_two_projects(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "two.db"))
    with TestClient(create_app(gateway=ScriptedGW())) as client:
        ok = client.post("/api/v1/stories", json={"title": "global"})
        assert ok.status_code == 201  # zero projects: global root
        client.post("/api/v1/projects", json={"name": "alpha", "root": init_repo(tmp_path, "a")})
        res = client.post("/api/v1/stories", json={"title": "x"})
        assert res.status_code == 201  # exactly one: unchanged
        client.post("/api/v1/projects", json={"name": "beta", "root": init_repo(tmp_path, "b")})
        res = client.post("/api/v1/stories", json={"title": "y"})
        assert res.status_code == 422
        assert "alpha" in res.json()["detail"] and "beta" in res.json()["detail"]
