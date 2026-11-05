"""CI first: while a merge-train batch has CI pending on the shared node, the
scheduler starts no story and pipeline verifies wait (30 min cap).

The hold is driven through the batch row (`ci_status` / `state` / `created_at`)
and the scheduler is stepped by calling `_tick` directly with the loop HARD-off:
no sleep-plus-deadline, no TRAIN_HOLD_FORMING, no wall-clock windows.
"""

import asyncio
import json
import logging
from datetime import timedelta

import pytest
from sqlmodel import select
from test_merge_train import _fake_train_gitea

from app import train as train_module
from app.events import bus
from app.models import Story, StoryEvent, TrainBatch, TrainBatchState, utcnow
from app.runner import StoryRunner

pytestmark = pytest.mark.timeout(60)


def _setup(client, tmp_path, server_port: int = 1):
    """Scheduler hard-off, a train project and one OPEN pending batch."""
    runner: StoryRunner = client.app.state.story_runner
    client.portal.call(runner.stop)
    token = tmp_path / "token.txt"
    token.write_text("sekrit\n")
    project = client.post(
        "/api/v1/projects",
        json={
            "name": "p",
            "root": str(tmp_path),
            "delivery": "push",
            "pr_api_base": f"http://127.0.0.1:{server_port}",
            "pr_repo": "acme/app",
            "pr_base_branch": "main",
            "pr_token_file": str(token),
            "merge_train": True,
        },
    ).json()
    with runner_session(runner) as db:
        batch = TrainBatch(
            project_id=project["id"],
            state=TrainBatchState.OPEN,
            pr_index=100,
            head_sha="abc123",
            ci_status="pending",
            created_at=utcnow(),
        )
        db.add(batch)
        db.commit()
        batch_id = batch.id
    return runner, project, batch_id


def runner_session(runner):
    from sqlmodel import Session

    return Session(runner.engine)


def _set_batch(runner, batch_id, **fields):
    with runner_session(runner) as db:
        batch = db.get(TrainBatch, batch_id)
        for key, value in fields.items():
            setattr(batch, key, value)
        db.add(batch)
        db.commit()


def _status(runner, story_id) -> str:
    with runner_session(runner) as db:
        return db.get(Story, story_id).status


def _no_judge(monkeypatch):
    """The tick's train pass would poll Gitea; the hold reads the row only."""
    monkeypatch.setattr(train_module, "train_tick", lambda db: None)


def _tick(client, runner):
    client.portal.call(runner._tick, 4)


def _queue(client, project_id) -> str:
    story = client.post(
        "/api/v1/stories", json={"title": "held", "project_id": project_id}
    ).json()
    assert client.post(f"/api/v1/stories/{story['id']}/queue").status_code == 200
    return story["id"]


def test_pending_ci_holds_pickup_until_it_ends(client, tmp_path, monkeypatch):
    runner, project, batch_id = _setup(client, tmp_path)
    _no_judge(monkeypatch)
    sid = _queue(client, project["id"])

    for _ in range(3):
        _tick(client, runner)
        assert _status(runner, sid) == "queued"

    _set_batch(runner, batch_id, ci_status="success")
    _tick(client, runner)
    assert _status(runner, sid) != "queued"


def test_failed_ci_also_lifts_the_hold(client, tmp_path, monkeypatch):
    runner, project, batch_id = _setup(client, tmp_path)
    _no_judge(monkeypatch)
    sid = _queue(client, project["id"])
    _tick(client, runner)
    assert _status(runner, sid) == "queued"
    _set_batch(runner, batch_id, ci_status="failure")
    _tick(client, runner)
    assert _status(runner, sid) != "queued"


def test_hold_cap_lifts_with_one_warning(client, tmp_path, monkeypatch, caplog):
    runner, project, batch_id = _setup(client, tmp_path)
    _no_judge(monkeypatch)
    _set_batch(
        runner,
        batch_id,
        created_at=utcnow() - timedelta(seconds=train_module.TRAIN_CI_HOLD_CAP_S + 5),
    )
    # two queued stories: three ticks reach the expired branch several times
    first = _queue(client, project["id"])
    second = _queue(client, project["id"])
    with caplog.at_level(logging.WARNING, logger="app.runner"):
        for _ in range(3):
            _tick(client, runner)
    assert _status(runner, first) != "queued"
    assert _status(runner, second) != "queued"
    lifted = [r for r in caplog.records if "train CI hold lifted" in r.getMessage()]
    assert len(lifted) == 1
    assert f"batch {batch_id} PR #100 never reported" in lifted[0].getMessage()


def test_hold_ignores_projects_without_the_train(client, tmp_path, monkeypatch):
    runner, project, batch_id = _setup(client, tmp_path)
    _no_judge(monkeypatch)
    with runner_session(runner) as db:
        assert train_module.train_ci_hold(db).id == batch_id
    assert client.patch(
        f"/api/v1/projects/{project['id']}", json={"merge_train": False}
    ).status_code == 200
    with runner_session(runner) as db:
        assert train_module.train_ci_hold(db) is None


def test_verify_waits_for_train_ci(client, tmp_path, monkeypatch):
    runner, project, batch_id = _setup(client, tmp_path)
    client.patch("/api/v1/settings", json={"verify_command": "true"})
    story = client.post(
        "/api/v1/stories", json={"title": "v", "project_id": project["id"]}
    ).json()
    sid = story["id"]
    ws = tmp_path / "ws"
    ws.mkdir()
    runner._tick_interval = 0.01

    async def coder_round(prompt: str) -> None:
        return None

    async def scenario():
        queue = await bus.subscribe()
        task = asyncio.create_task(
            runner._verify_and_fix(sid, str(tmp_path), ws, coder_round, "v", None)
        )
        async def read_frames():
            while True:
                frame = json.loads(await queue.get())
                if (
                    frame.get("kind") == "story_log"
                    and "waiting for train CI" in frame["data"]["text"]
                ):
                    assert "PR #100" in frame["data"]["text"]
                    return

        try:
            try:
                await asyncio.wait_for(read_frames(), timeout=30)
            except TimeoutError:
                pytest.fail("no 'waiting for train CI' frame")
            with runner_session(runner) as db:
                kinds = [
                    e.kind
                    for e in db.exec(
                        select(StoryEvent).where(StoryEvent.story_id == sid)
                    ).all()
                ]
            assert "verify" not in kinds
            assert not task.done()
            _set_batch(runner, batch_id, ci_status="success")
            try:
                result = await asyncio.wait_for(task, timeout=30)
            except TimeoutError:
                pytest.fail("verify never finished after train CI went green")
        finally:
            bus.unsubscribe(queue)
            if not task.done():
                task.cancel()
        return result

    result = client.portal.call(scenario)
    assert result["passed"] is True
    with runner_session(runner) as db:
        events = db.exec(
            select(StoryEvent).where(StoryEvent.story_id == sid)
        ).all()
    kinds = [e.kind for e in events]
    assert "verify" in kinds
    note = next(
        json.loads(e.data) if isinstance(e.data, str) else e.data
        for e in events
        if e.kind == "note"
    )
    assert note == {"waiting_for_train_ci": True, "pr_index": 100}
    assert sid not in runner._watchdog_floor


def test_judge_stores_ci_status_from_gitea(client, tmp_path):
    server = _fake_train_gitea()
    try:
        runner, project, batch_id = _setup(
            client, tmp_path, server.server_address[1]
        )
        branch = f"train/{batch_id}"
        server.branches[branch] = "abc123"
        server.pull_heads[100] = "abc123"
        server.pulls[100] = {"merged": False, "mergeable": True, "state": "open"}

        def ci_status() -> str | None:
            with runner_session(runner) as db:
                train_module.train_tick(db)
            with runner_session(runner) as db:
                return db.get(TrainBatch, batch_id).ci_status

        server.statuses["abc123"] = {"status": "pending", "statuses": []}
        assert ci_status() == "pending"
        server.statuses["abc123"] = {
            "status": "success",
            "statuses": [{"status": "success"}],
        }
        assert ci_status() == "success"
        with runner_session(runner) as db:
            assert train_module.train_ci_hold(db) is None
        # an unreadable status (empty answer) leaves the stored value alone
        server.statuses.pop("abc123")
        assert ci_status() == "success"
    finally:
        server.shutdown()
