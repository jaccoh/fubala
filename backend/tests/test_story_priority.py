"""Urgent priority: urgent queued stories go ahead of all normal ones."""

import asyncio
import json
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, SQLModel

from app import mcp_tools as mcp
from app import models  # noqa: F401  (registers the tables on the metadata)
from app.main import _migrate, create_app
from app.models import Story, StoryStatus, utcnow
from app.runner import STARVATION_S, queued_pickup_order

API = "/api/v1/stories"


def _client(tmp_path, monkeypatch, gateway) -> TestClient:
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "prio.db"))
    return TestClient(create_app(gateway=gateway))


def _seed(client, title, *, priority="normal", skill="medium", age_s=0.0, status="queued"):
    """Create a story over the API, then pin status/skill/queued_at in the DB."""
    sid = client.post(API, json={"title": title, "priority": priority}).json()["id"]
    with Session(client.app.state.engine) as db:
        story = db.get(Story, sid)
        story.status = status
        story.skill = skill
        story.queued_at = utcnow() - timedelta(seconds=age_s)
        db.add(story)
        db.commit()
    return sid


def _order(client) -> list[str]:
    with Session(client.app.state.engine) as db:
        return [s.id for s in queued_pickup_order(db)]


def test_urgent_after_ai_review_requeue_beats_older_normals(tmp_path, monkeypatch, gateway):
    with _client(tmp_path, monkeypatch, gateway) as client:
        client.portal.call(client.app.state.story_runner.stop)
        starved = _seed(client, "starved", age_s=STARVATION_S + 60)
        cheap = _seed(client, "cheap", skill="low", age_s=120)
        urgent = _seed(
            client, "urgent", priority="urgent", skill="high", status="ai_review"
        )
        # the AI-review requeue path: fresh queued_at, nothing else reset
        client.app.state.story_runner._finish(urgent, StoryStatus.QUEUED, StoryStatus.AI_REVIEW)
        assert _order(client) == [urgent, starved, cheap]
        with Session(client.app.state.engine) as db:
            assert db.get(Story, urgent).priority == "urgent"


def test_urgent_is_fifo_ignoring_skill_tier(tmp_path, monkeypatch, gateway):
    with _client(tmp_path, monkeypatch, gateway) as client:
        client.portal.call(client.app.state.story_runner.stop)
        later_cheap = _seed(client, "later", priority="urgent", skill="low", age_s=10)
        earlier_high = _seed(client, "earlier", priority="urgent", skill="high", age_s=100)
        normal = _seed(client, "normal", age_s=1000)
        assert _order(client) == [earlier_high, later_cheap, normal]


def test_queue_position_matches_pickup_order(tmp_path, monkeypatch, gateway):
    with _client(tmp_path, monkeypatch, gateway) as client:
        client.portal.call(client.app.state.story_runner.stop)
        normal = _seed(client, "normal", age_s=500)
        urgent = _seed(client, "urgent", priority="urgent", age_s=5)
        order = _order(client)
        assert order == [urgent, normal]
        listed = {s["id"]: s for s in client.get(API).json()}
        for position, sid in enumerate(order, start=1):
            assert listed[sid]["queue_position"] == position
        assert listed[urgent]["priority"] == "urgent"
        assert listed[normal]["priority"] == "normal"


def test_patch_priority(tmp_path, monkeypatch, gateway):
    with _client(tmp_path, monkeypatch, gateway) as client:
        client.portal.call(client.app.state.story_runner.stop)
        reviewing = _seed(client, "reviewing", status="ai_review")
        res = client.patch(f"{API}/{reviewing}", json={"priority": "urgent"})
        assert res.status_code == 200
        assert res.json()["priority"] == "urgent"
        events = client.get(f"{API}/{reviewing}").json()["events"]
        notes = [
            json.loads(e["data"]) if isinstance(e["data"], str) else e["data"]
            for e in events
            if e["kind"] == "note"
        ]
        assert {"priority": True, "from": "normal", "to": "urgent", "by": "operator"} in notes

        assert client.patch(f"{API}/{reviewing}", json={"priority": "asap"}).status_code == 422
        assert (
            client.post(API, json={"title": "x", "priority": "asap"}).status_code == 422
        )

        done = _seed(client, "done", status="done")
        assert client.patch(f"{API}/{done}", json={"priority": "urgent"}).status_code == 409


def test_mcp_priority_reaches_post_and_patch_bodies(monkeypatch):
    seen: dict = {}
    story = {
        "id": "s1", "title": "t", "status": "backlog", "depends_on": [],
        "spec_version": 1, "model": "m", "priority": "urgent",
    }

    async def fake_post(path, body=None):
        seen["post"] = body
        return story

    async def fake_patch(path, body=None):
        seen["patch"] = body
        return story

    async def fake_get(path):
        return []

    monkeypatch.setattr(mcp, "_post", fake_post)
    monkeypatch.setattr(mcp, "_patch", fake_patch)
    monkeypatch.setattr(mcp, "_get", fake_get)

    out = asyncio.run(mcp.create_story("t", priority="urgent"))
    assert seen["post"]["priority"] == "urgent"
    assert "priority: urgent" in out
    asyncio.run(mcp.update_story("s1", priority="urgent"))
    assert seen["patch"] == {"priority": "urgent"}

    asyncio.run(mcp.create_story("t"))
    assert "priority" not in seen["post"]


def test_migration_gives_existing_rows_normal_priority(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    SQLModel.metadata.create_all(engine)
    now = "2026-10-01 00:00:00"
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE stories DROP COLUMN priority"))
        conn.execute(text(
            "INSERT INTO stories (id, title, description, acceptance, status, depends_on, "
            "created_by, kind, spec_version, spec_rejects, created_at, updated_at) "
            f"VALUES ('s1', 't', '', '', 'queued', '[]', 'test', 'code', 1, 0, '{now}', '{now}')"
        ))
    _migrate(engine)
    assert "priority" in {c["name"] for c in inspect(engine).get_columns("stories")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT priority FROM stories")).scalar() == "normal"
