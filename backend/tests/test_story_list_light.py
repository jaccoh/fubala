"""GET /stories stays a light list: no events / code_diff / evidence, and fast
with a few hundred stories (the full object is GET /stories/{id} only)."""

from __future__ import annotations

import json
import time

from sqlmodel import Session

from app.models import Story, StoryEvent, StoryStatus

HEAVY_KEYS = ("events", "code_diff", "evidence")


def test_story_list_is_light_and_fast(client):
    with Session(client.app.state.engine) as db:
        for i in range(500):
            story = Story(
                id=f"light{i:04d}",
                title=f"story {i}",
                status=StoryStatus.HUMAN_REVIEW if i % 2 else StoryStatus.BACKLOG,
                code_diff="diff --git a b\n" + "+x\n" * 200,
                evidence=json.dumps({"code_diff": "d" * 500, "verify": "ok", "summary": "s"}),
            )
            db.add(story)
            for kind in ("created", "note", "verify"):
                db.add(StoryEvent(story_id=story.id, kind=kind, data=json.dumps({"n": i})))
        db.commit()

    client.get("/api/v1/stories")  # warm-up
    best = float("inf")
    items: list[dict] = []
    for _ in range(3):
        started = time.perf_counter()
        res = client.get("/api/v1/stories")
        best = min(best, time.perf_counter() - started)
        assert res.status_code == 200
        items = res.json()
    assert len(items) >= 500
    for item in items:
        for key in HEAVY_KEYS:
            assert key not in item, f"{key} leaked into the story list"
    assert best < 0.2, f"story list took {best * 1000:.0f} ms (best of 3)"

    # the full object stays one request away
    detail = client.get("/api/v1/stories/light0001").json()
    assert "events" in detail and "code_diff" in detail
