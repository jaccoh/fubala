"""Regression: a story sitting at the human gate WITH an open pull request
approves back into `landing` (idempotent re-land) instead of 409ing.

Why that could break: entering `landing` discards the worktree (the branch
lives on the remote), so the naive approve path would re-run `workspaces.push`,
die on "branch does not exist" and surface a 409 — leaving the operator no way
to re-arm the watch. Kept here, in its own file, so the behaviour stays pinned.
"""

from fastapi.testclient import TestClient

from app import runner as runner_module
from app.main import create_app

from tests.test_delivery_push import (
    ScriptedGW,
    _fake_gitea,
    init_remote,
    init_repo,
    wait_status,
)
from tests.test_failed_lane import _force_story_row


def test_approve_at_gate_with_open_pr_relands_without_pushing(tmp_path, monkeypatch):
    """A gate story whose PR is already open re-enters landing on approve —
    no second push, no second pull request."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    # mergeable False: the poller will bounce the story back to this gate
    # with a merge conflict, which is exactly the state under test
    server = _fake_gitea(pulls={7: {"merged": False, "mergeable": False, "state": "open"}})
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "reland.db"))
    # sweep Gitea every tick; the conflict DWELL still applies (2s), so the
    # bounce-back is the real, patient verdict rather than a fast-poll fluke
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
            client.post(f"/api/v1/stories/{sid}/queue")

            # spec gate -> executing -> code gate
            wait_status(client, sid, ("human_review", "failed"))
            client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
            gate = wait_status(client, sid, ("human_review", "failed"))
            assert gate["status"] == "human_review", gate.get("error")

            # first land: push + PR, then the poller bounces it back
            landed = client.post(
                f"/api/v1/stories/{sid}/approve", json={"by": "t"}
            )
            assert landed.status_code == 200, landed.text
            assert landed.json()["status"] == "landing", landed.json()

            bounced = wait_status(client, sid, ("human_review",), timeout=30.0)
            assert bounced["error"] == "land failed: merge conflict"

            # THE regression: approving again must not 409 — it re-lands
            again = client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
            assert again.status_code == 200, again.text
            body = again.json()
            assert body["status"] == "landing", body
            assert body["error"] is None
            assert body["pr_index"] == 7

            # and it re-armed the existing PR rather than opening a new one:
            # exactly one create POST, and no branch-update POST
            creates = [
                r
                for r in server.requests
                if r["method"] == "POST" and r["path"].endswith("/pulls")
            ]
            assert len(creates) == 1, server.requests
            updates = [
                r
                for r in server.requests
                if r["method"] == "POST" and r["path"].split("?")[0].endswith("/update")
            ]
            assert updates == []

            # the watch still works: once the PR merges the story is done
            server.pulls[7] = {"merged": True, "mergeable": True, "state": "open"}
            done = wait_status(client, sid, ("done",))
            assert done["error"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_retry_stage_landing_resumes_a_failed_story_with_a_pr(tmp_path, monkeypatch):
    """POST /retry {"stage": "landing"} on a failed push+PR story re-arms the
    landing watch directly; without a pull request the same call is a 409."""
    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(pulls={7: {"merged": False, "mergeable": True, "state": "open"}})
    db_path = tmp_path / "reland-retry.db"
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

            # a failed story that carries a live PR (as a land failure would):
            # forced via the row so the failure shape is exact
            _force_story_row(
                db_path,
                story_id=sid,
                status="failed",
                error="land failed: merge conflict",
                pr_index=7,
                pr_url="http://gitea.local/acme/app/pulls/7",
            )

            # without a pull request the same call must refuse first
            bare = tmp_path / "bare.db"
            monkeypatch.setenv("FUBALA_DATABASE_PATH", str(bare))
            with TestClient(create_app(gateway=ScriptedGW())) as client2:
                client2.patch(
                    "/api/v1/settings",
                    json={
                        "story_poll_interval": 0.05,
                        "project_root": repo,
                        "write_tools_enabled": True,
                    },
                )
                project2 = client2.post(
                    "/api/v1/projects",
                    json={
                        "name": "p2",
                        "root": repo,
                        "delivery": "push",
                        "pr_api_base": f"http://127.0.0.1:{server.server_address[1]}",
                        "pr_repo": "acme/app",
                        "pr_base_branch": "main",
                        "pr_token_file": str(token_file),
                    },
                ).json()
                story2 = client2.post(
                    "/api/v1/stories",
                    json={"title": "feature", "project_id": project2["id"]},
                ).json()
                sid2 = story2["id"]
                _force_story_row(bare, story_id=sid2, status="failed")
                denied = client2.post(
                    f"/api/v1/stories/{sid2}/retry", json={"stage": "landing", "by": "t"}
                )
                assert denied.status_code == 409, denied.text
                assert "no pull request" in denied.json()["detail"]

            # back to the first app: retry lands the story on its PR
            monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
            retried = client.post(
                f"/api/v1/stories/{sid}/retry", json={"stage": "landing", "by": "t"}
            )
            assert retried.status_code == 200, retried.text
            body = retried.json()
            assert body["status"] == "landing", body
            assert body["error"] is None
            assert body["pr_index"] == 7

            # the re-armed watch carries the story to done on merge
            server.pulls[7] = {"merged": True, "mergeable": True, "state": "open"}
            done = wait_status(client, sid, ("done",))
            assert done["error"] is None
            assert done["pr_index"] == 7
    finally:
        server.shutdown()
        server.server_close()


def test_approve_of_a_main_moved_gate_story_updates_the_branch_and_lands(
    tmp_path, monkeypatch
):
    """A gate story whose error says main moved is approved: the branch is
    updated first, the moved head goes green and merged, the story is done."""
    from tests.test_delivery_push import _push_project

    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sekrit\n")
    server = _fake_gitea(
        pulls={7: {"merged": False, "mergeable": True, "state": "open"}},
        pull_heads={7: "aaa"},
    )
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "moved.db"))
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
            sid = client.post(
                "/api/v1/stories", json={"title": "feature", "project_id": pid}
            ).json()["id"]
            _force_story_row(
                str(tmp_path / "moved.db"),
                story_id=sid,
                status="human_review",
                spec="spec",
                base_ref="main",
                error="land failed: main moved during land (3 re-land attempts used)",
                pr_index=7,
                pr_url="http://gitea.local/acme/app/pulls/7",
            )
            resp = client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "landing"
            assert resp.json()["error"] is None
            updates = [
                r
                for r in server.requests
                if r["method"] == "POST" and r["path"].split("?")[0].endswith("/update")
            ]
            assert len(updates) == 1, server.requests
            assert server.pull_heads[7] == "aaa-updated"
            server.pulls[7] = {"merged": True, "mergeable": True, "state": "open"}
            done = wait_status(client, sid, ("done",))
            assert done["error"] is None
    finally:
        server.shutdown()
        server.server_close()
