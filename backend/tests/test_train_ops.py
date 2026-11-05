"""Merge-train operations from a story/agent: view, pause/resume, rerun, bounce,
land-now and force-merge, over REST and MCP, against the fake Gitea of
test_merge_train (real git underneath, a scripted remote on top).

The fake cannot reproduce the real `ci / test` check or the land job's .gitea
refusal: these tests prove Fubala's own guard only. It treats a POST it does
not route as a PR create, so every test that merges or reruns also asserts
`pr_creates`."""

import contextlib

import pytest
from test_mcp_http import call, call_raw
from test_merge_train import (
    Train,
    _assert_retired,
    _close_clients,
    _count_train_ticks,
    _depart,
    _fake_train_gitea,
    _make_train,
    _run_stories_together,
    _stop_server,
    wait_for,
)

pytestmark = pytest.mark.timeout(120)


@contextlib.contextmanager
def train_env(tmp_path, monkeypatch, members=2, *, open_batch=True):
    """A project with the train on and `members` stories landed in ONE batch:
    open (default) or still forming, held there by the formation flag."""
    server = _fake_train_gitea()
    client = None
    try:
        client, project, repo, _bare = _make_train(
            tmp_path, monkeypatch, server, window=1
        )
        t = Train(client, server, project["id"])
        t._repo = repo
        sids = _run_stories_together(client, project["id"], members)
        if open_batch:
            batch = _depart(t, members, what="the batch to open")
            t.seed_batch_pr("")  # point the remote at the head, no statuses yet
            assert server.pr_creates == 1
        else:
            batch = next(b for b in t.batches() if b["state"] == "forming")
        yield t, server, sids, batch
    finally:
        if client:
            _close_clients(client)
        _stop_server(server)


def server_url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def _ci(t, batch: dict, contexts: dict[str, str], combined: str = "pending") -> None:
    """Stamp per-context CI on the batch head. The combined status stays
    pending on purpose: the poller must neither re-run nor bisect the batch
    while the test works; the operator guard reads the per-context list."""
    t.server.statuses[batch["head_sha"]] = {
        "status": combined,
        "statuses": [
            {"context": c, "status": state, "description": state}
            for c, state in contexts.items()
        ],
    }


def _post(t, path: str, body: dict):
    return t.client.post(f"/api/v1/projects/{t.project_id}/train/{path}", json=body)


def _notes(t, sid: str) -> list[dict]:
    return t.story(sid)["_notes"]


def _note_with(t, sid: str, key: str) -> dict:
    found = [n for n in _notes(t, sid) if n.get(key)]
    assert found, (key, _notes(t, sid))
    return found[-1]


def test_view_reports_members_contexts_and_timing(tmp_path, monkeypatch):
    with train_env(tmp_path, monkeypatch) as (t, server, sids, batch):
        _ci(t, batch, {"ci / test": "success", "lint": "pending"})
        rows = t.client.get(
            f"/api/v1/projects/{t.project_id}/train?contexts=true"
        ).json()
        assert isinstance(rows, list)
        row = next(b for b in rows if b["id"] == batch["id"])
        assert sorted(m["id"] for m in row["members"]) == sorted(sids)
        assert row["pr_index"] == batch["pr_index"]
        assert row["head_sha"] == batch["head_sha"]
        assert row["ci_contexts"] == {"ci / test": "success", "lint": "pending"}
        assert row["state"] == "open"
        assert row["red_runs"] == 0 and row["infra_retries"] == 0
        assert "next_attempt_at" in row and row["age_s"] >= 0
        assert "ci_status" in row
        # the board's poll: no contexts unless asked for
        plain = t.client.get(f"/api/v1/projects/{t.project_id}/train").json()
        assert all("ci_contexts" not in b for b in plain)
        assert len(t.client.get(f"/api/v1/projects/{t.project_id}/train?limit=1").json()) == 1
        assert server.pr_creates == 1


def test_view_contexts_is_null_when_gitea_fails(tmp_path, monkeypatch):
    with train_env(tmp_path, monkeypatch) as (t, server, sids, batch):
        # a Gitea error on the statuses read: null, never a 500
        from app import pr as pr_module

        def boom(*args, **kwargs):
            raise pr_module.PullRequestError("boom")

        monkeypatch.setattr(pr_module, "all_statuses", boom)
        rows = t.client.get(
            f"/api/v1/projects/{t.project_id}/train?contexts=true"
        ).json()
        row = next(b for b in rows if b["id"] == batch["id"])
        assert row["ci_contexts"] is None


def test_pause_holds_the_flush_and_resume_releases_it(tmp_path, monkeypatch):
    with train_env(tmp_path, monkeypatch, open_batch=False) as (t, server, sids, _b):
        assert _post(t, "pause", {"by": "ops", "reason": ""}).status_code == 422
        assert _post(t, "pause", {"by": " ", "reason": "x"}).status_code == 422
        assert _post(t, "pause", {"by": "ops"}).status_code == 422
        paused = _post(t, "pause", {"by": "ops", "reason": "release freeze"})
        assert paused.status_code == 200, paused.text
        body = paused.json()
        assert body["train_paused"] is True
        assert body["train_paused_by"] == "ops"
        assert body["train_pause_reason"] == "release freeze"
        assert body["train_paused_at"]
        for sid in sids:
            note = _note_with(t, sid, "train_paused")
            assert note["by"] == "ops" and note["reason"] == "release freeze"

        from app import train as train_module

        ticks = _count_train_ticks(monkeypatch)
        train_module.TRAIN_HOLD_FORMING = False  # the batch is due now
        seen = ticks[0]
        wait_for(lambda: ticks[0] >= seen + 4, what="ticks while paused")
        assert [b["state"] for b in t.batches()] == ["forming"]
        assert server.pr_creates == 0

        resumed = _post(t, "resume", {"by": "ops2", "reason": "freeze over"})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["train_paused"] is False
        assert resumed.json()["train_paused_by"] is None
        for sid in sids:
            assert _note_with(t, sid, "train_resumed")["by"] == "ops2"
        wait_for(lambda: t.open_batches(), what="the batch to flush after resume")
        assert server.pr_creates == 1
        assert _post(t, "resume", {"by": ""}).status_code == 422


def test_rerun_reruns_ci_on_the_same_batch(tmp_path, monkeypatch):
    with train_env(tmp_path, monkeypatch) as (t, server, sids, batch):
        path = f"batches/{batch['id']}/rerun"
        assert _post(t, path, {"by": "ops"}).status_code == 409  # no run yet
        assert server.rerun_calls == []
        server.runs.append({"id": 41, "head_sha": "older"})
        server.runs.append({"id": 77, "head_sha": batch["head_sha"]})
        assert _post(t, path, {"by": ""}).status_code == 422
        res = _post(t, path, {"by": "ops"})
        assert res.status_code == 200, res.text
        assert server.rerun_calls == [77]
        assert res.json()["id"] == batch["id"]
        assert res.json()["ci_status"] == "pending"
        again = next(b for b in t.batches() if b["id"] == batch["id"])
        assert again["pr_index"] == batch["pr_index"]
        assert again["head_sha"] == batch["head_sha"]
        for sid in sids:
            note = _note_with(t, sid, "rerun")
            assert note["by"] == "ops"
            assert note["train_batch_id"] == batch["id"]
            assert note["pr_index"] == batch["pr_index"]
            assert note["head_sha"] == batch["head_sha"]
            assert note["run_id"] == 77
        assert server.pr_creates == 1
        assert _post(t, "batches/nope/rerun", {"by": "ops"}).status_code == 404


def test_rerun_refuses_a_forming_batch(tmp_path, monkeypatch):
    with train_env(tmp_path, monkeypatch, open_batch=False) as (t, server, _s, batch):
        assert _post(t, f"batches/{batch['id']}/rerun", {"by": "ops"}).status_code == 409
        assert server.rerun_calls == [] and server.pr_creates == 0


def test_bounce_an_open_member_re_forms_the_rest(tmp_path, monkeypatch):
    with train_env(tmp_path, monkeypatch, members=3) as (t, server, sids, batch):
        path = f"batches/{batch['id']}/bounce"
        outsider = {"by": "ops", "story_id": "not-a-member"}
        assert _post(t, path, outsider).status_code == 404
        res = _post(t, path, {"by": "ops", "story_id": sids[0], "reason": "breaks lint"})
        assert res.status_code == 200, res.text
        story = t.client.get(f"/api/v1/stories/{sids[0]}").json()
        assert story["status"] == "human_review"
        note = _note_with(t, sids[0], "bounced")
        assert note["by"] == "ops" and note["reason"] == "breaks lint"
        rows = t.batches()
        old = next(b for b in rows if b["id"] == batch["id"])
        assert old["state"] == "red"
        child = next(b for b in rows if b["parent_batch_id"] == batch["id"])
        assert sorted(m["id"] for m in child["members"]) == sorted(sids[1:])
        assert child["red_runs"] == 0
        for sid in sids[1:]:
            assert _note_with(t, sid, "parent_batch_id")["train_batch_id"] == child["id"]
        _assert_retired(server, batch)


def test_bounce_in_a_forming_batch_only_drops_the_member(tmp_path, monkeypatch):
    with train_env(tmp_path, monkeypatch, open_batch=False) as (t, server, sids, batch):
        res = _post(
            t,
            f"batches/{batch['id']}/bounce",
            {"by": "ops", "story_id": sids[0]},
        )
        assert res.status_code == 200, res.text
        assert t.client.get(f"/api/v1/stories/{sids[0]}").json()["status"] == "human_review"
        forming = next(b for b in t.batches() if b["id"] == batch["id"])
        assert [m["id"] for m in forming["members"]] == [sids[1]]
        assert _note_with(t, sids[0], "bounced")["by"] == "ops"
        assert server.pr_creates == 0


def test_land_now_merges_green_and_refuses_the_rest(tmp_path, monkeypatch):
    with train_env(tmp_path, monkeypatch) as (t, server, sids, batch):
        path = f"batches/{batch['id']}/land-now"
        _ci(t, batch, {"ci / test": "pending"})
        refused = _post(t, path, {"by": "ops"})
        assert refused.status_code == 409 and "ci / test" in refused.text
        assert server.merges == []

        # no readable protection: ALL contexts must be green
        _ci(t, batch, {"ci / test": "success", "lint": "failure"})
        assert _post(t, path, {"by": "ops"}).status_code == 409
        assert server.merges == []

        # readable protection: only the required contexts count
        server.protections["main"] = {
            "enable_status_check": True,
            "status_check_contexts": ["ci / test"],
        }
        server.pull_files[batch["pr_index"]] = [".gitea/workflows/ci.yml"]
        gated = _post(t, path, {"by": "ops"})
        assert gated.status_code == 409 and "force-merge" in gated.text
        assert server.merges == []

        server.pull_files[batch["pr_index"]] = ["app/x.py"]
        assert _post(t, path, {"by": ""}).status_code == 422
        res = _post(t, path, {"by": "ops"})
        assert res.status_code == 200, res.text
        assert server.merges == [
            {
                "index": batch["pr_index"],
                "head_commit_id": batch["head_sha"],
                "force_merge": False,
            }
        ]
        for sid in sids:
            note = _note_with(t, sid, "land_now")
            assert note["by"] == "ops"
            assert note["pr_index"] == batch["pr_index"]
            assert "ci / test" in note["green_rule"]
        # the CI land job may have won the race: already merged is success
        from app import pr as pr_module

        pr_module.merge_pull(
            server_url(server), "acme/app", "tok", batch["pr_index"], batch["head_sha"]
        )
        assert len(server.merges) == 1
        assert server.pr_creates == 1


def test_merge_pull_raises_on_a_refusal(tmp_path, monkeypatch):
    from app import pr as pr_module

    with train_env(tmp_path, monkeypatch) as (t, server, _s, batch):
        with pytest.raises(pr_module.PullRequestError):
            pr_module.merge_pull(server_url(server), "acme/app", "tok", 9999, "x")


def test_land_now_refuses_a_forming_batch_and_an_empty_ci(tmp_path, monkeypatch):
    with train_env(tmp_path, monkeypatch, open_batch=False) as (t, server, _s, batch):
        res = _post(t, f"batches/{batch['id']}/land-now", {"by": "ops"})
        assert res.status_code == 409 and "batch has no PR/CI yet" in res.text
    (tmp_path / "b").mkdir()
    with train_env(tmp_path / "b", monkeypatch) as (t, server, _s, batch):
        # no contexts at all is not green
        res = _post(t, f"batches/{batch['id']}/land-now", {"by": "ops"})
        assert res.status_code == 409
        assert server.merges == [] and server.pr_creates == 1


def test_force_merge_lands_a_workflow_pr_with_a_reason(tmp_path, monkeypatch):
    with train_env(tmp_path, monkeypatch) as (t, server, sids, batch):
        path = f"batches/{batch['id']}/force-merge"
        server.pull_files[batch["pr_index"]] = [".gitea/workflows/ci.yml"]
        body = {"by": "ops", "reason": "workflow change, reviewed by hand"}
        assert _post(t, path, {"by": "ops"}).status_code == 422
        assert _post(t, path, {"by": "ops", "reason": " "}).status_code == 422
        assert _post(t, path, {"by": " ", "reason": "r"}).status_code == 422

        _ci(t, batch, {"ci / test": "failure"})
        assert _post(t, path, body).status_code == 409

        server.protections["main"] = {
            "enable_status_check": True,
            "status_check_contexts": ["ci / test"],
        }
        _ci(t, batch, {"lint": "success"})  # required context missing
        missing = _post(t, path, body)
        assert missing.status_code == 409 and "missing" in missing.text
        assert server.merges == []

        _ci(t, batch, {"ci / test": "success", "lint": "failure"})
        res = _post(t, path, body)
        assert res.status_code == 200, res.text
        assert server.merges == [
            {
                "index": batch["pr_index"],
                "head_commit_id": batch["head_sha"],
                "force_merge": True,
            }
        ]
        for sid in sids:
            note = _note_with(t, sid, "force_merge")
            assert note["by"] == "ops"
            assert note["reason"] == body["reason"]
            assert note["pr_index"] == batch["pr_index"]
            assert note["pr_url"] == batch["pr_url"]
            assert note["head_sha"] == batch["head_sha"]
        assert server.pr_creates == 1


def test_mcp_train_tools_refuse_an_empty_by_and_roundtrip(tmp_path, monkeypatch):
    with train_env(tmp_path, monkeypatch) as (t, server, sids, batch):
        pid, bid = t.project_id, batch["id"]
        for name, args in (
            ("pause_project_train", {"reason": "x"}),
            ("resume_project_train", {}),
            ("rerun_train_batch", {"batch_id": bid}),
            ("bounce_train_member", {"batch_id": bid, "story_id": sids[0]}),
            ("land_train_batch_now", {"batch_id": bid}),
            ("force_merge_train_batch", {"batch_id": bid, "reason": "x"}),
        ):
            out = call(t.client, name, {"project_id": pid, **args, "by": "  "})
            assert "'by' is required" in out, name
        assert server.merges == [] and server.rerun_calls == []
        assert t.client.get(f"/api/v1/stories/{sids[0]}").json()["status"] == "landing"

        paused = call_raw(
            t.client, "pause_project_train", {"project_id": pid, "by": "bot", "reason": "r"}
        )
        assert paused["train_paused"] is True
        assert "Train paused by bot: r" in call(
            t.client, "get_project_train", {"project_id": pid}
        )
        resumed = call_raw(t.client, "resume_project_train", {"project_id": pid, "by": "bot"})
        assert resumed["train_paused"] is False

        _ci(t, batch, {"ci / test": "success"})
        merged = call_raw(
            t.client,
            "land_train_batch_now",
            {"project_id": pid, "batch_id": bid, "by": "bot"},
        )
        assert merged["id"] == bid
        assert len(server.merges) == 1


def _force_ready(t, server, batch) -> str:
    server.pull_files[batch["pr_index"]] = [".gitea/workflows/ci.yml"]
    server.protected_files = [".gitea/*"]
    _ci(t, batch, {"ci / test": "success"})
    return f"batches/{batch['id']}/force-merge"


def test_plain_merge_of_a_protected_pr_raises_protected_files_error(
    tmp_path, monkeypatch
):
    from app import pr as pr_module

    with train_env(tmp_path, monkeypatch) as (t, server, _s, batch):
        _force_ready(t, server, batch)
        with pytest.raises(pr_module.ProtectedFilesError):
            pr_module.merge_pull(
                server_url(server),
                "acme/app",
                "tok",
                batch["pr_index"],
                batch["head_sha"],
            )
        assert server.merges == []


def test_force_merge_without_the_right_is_a_409_with_a_note(tmp_path, monkeypatch):
    with train_env(tmp_path, monkeypatch) as (t, server, sids, batch):
        path = _force_ready(t, server, batch)
        server.force_allowed = False
        res = _post(t, path, {"by": "ops", "reason": "workflow change"})
        assert res.status_code == 409, res.text
        assert (
            "the PR token may not merge protected files: give it that right "
            "or merge by hand" in res.text
        )
        assert server.merges == []
        for sid in sids:
            note = _note_with(t, sid, "force_merge_refused")
            assert note["by"] == "ops" and note["reason"] == "workflow change"
            assert note["pr_index"] == batch["pr_index"]
            assert note["head_sha"] == batch["head_sha"]
            assert "protected files" in note["error"]
        assert t.batches()[0]["state"] == "open"
        assert t.client.get(f"/api/v1/stories/{sids[0]}").json()["status"] == "landing"


def test_force_merge_with_a_moved_pr_head_merges_nothing(tmp_path, monkeypatch):
    with train_env(tmp_path, monkeypatch) as (t, server, _sids, batch):
        path = _force_ready(t, server, batch)
        # _ci_green refreshes the head from pull_heads; pin the fake to a head
        # the request cannot match
        server.pull_heads[batch["pr_index"]] = "f" * 40
        res = _post(t, path, {"by": "ops", "reason": "r"})
        assert res.status_code != 200, res.text
        assert server.merges == []


def test_force_merge_then_ff_lands_every_member(tmp_path, monkeypatch):
    from test_merge_train import _git

    with train_env(tmp_path, monkeypatch) as (t, server, sids, batch):
        path = _force_ready(t, server, batch)
        res = _post(t, path, {"by": "ops", "reason": "workflow change"})
        assert res.status_code == 200, res.text
        assert server.merges[0]["force_merge"] is True
        assert server.merges[0]["head_commit_id"] == batch["head_sha"]
        repo = t._repo
        _git(repo, "fetch", "-q", "origin")
        _git(repo, "merge", "--ff-only", f"origin/train/{batch['id']}")
        _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(repo, "reset", "-q", "--hard", "origin/main")
        wait_for(
            lambda: all(
                t.client.get(f"/api/v1/stories/{sid}").json()["status"] == "done"
                for sid in sids
            ),
            what="every member done",
        )
