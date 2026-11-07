"""GET /stories stays a light list: no events / code_diff / evidence, and a
constant number of queries with hundreds of stories (the full object is
GET /stories/{id} only)."""

from __future__ import annotations

import json
from contextlib import contextmanager

from sqlalchemy import event
from sqlmodel import Session

from app.models import Story, StoryEvent, StoryStatus

HEAVY_KEYS = ("events", "code_diff", "evidence")


# a list request runs a handful of statements; 500 stories must not scale it
MAX_LIST_QUERIES = 10


@contextmanager
def count_statements(engine):
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_story_list_is_light_and_fast(client):
    # scheduler hard-off: its polling would add statements to the count
    client.portal.call(client.app.state.story_runner.stop)
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
    with count_statements(client.app.state.engine) as statements:
        res = client.get("/api/v1/stories")
    assert res.status_code == 200
    items = res.json()
    assert len(items) >= 500
    sample = "\n".join(statements[:5])
    assert len(statements) <= MAX_LIST_QUERIES, (
        f"story list ran {len(statements)} statements, first:\n{sample}"
    )
    event_queries = [q for q in statements if "storyevent" in q.lower()]
    assert len(event_queries) <= 1, (
        f"story list ran {len(event_queries)} storyevent statements, first:\n"
        + "\n".join(event_queries[:5])
    )
    for item in items:
        for key in HEAVY_KEYS:
            assert key not in item, f"{key} leaked into the story list"

    # the full object stays one request away
    detail = client.get("/api/v1/stories/light0001").json()
    assert "events" in detail and "code_diff" in detail
