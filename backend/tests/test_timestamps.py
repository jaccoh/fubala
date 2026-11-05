"""UTC-offset serialization: every datetime the API emits carries an offset.

SQLite round trips hand back naive datetimes (the stored values are UTC, the
tzinfo is lost), so the offset must be attached at the serialization boundary
— both the Pydantic response models and the hand-built SSE payloads
(`story_payload`, note dicts).
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.events import bus
from app.models import Story
from app.runner import story_payload


def _assert_utc_iso(iso: str) -> datetime:
    """`Z` or `+00:00`, parseable, and within ~5s of now (fresh rows)."""
    assert iso.endswith("Z") or iso.endswith("+00:00"), iso
    parsed = datetime.fromisoformat(iso)
    assert parsed.utcoffset() == timedelta(0), iso
    assert abs(parsed - datetime.now(UTC)) < timedelta(seconds=5), iso
    return parsed


def _create_story(client, title: str) -> str:
    return client.post("/api/v1/stories", json={"title": title}).json()["id"]


def test_story_detail_and_list_timestamps_carry_utc_offset(client):
    story_id = _create_story(client, "ts rest")

    detail = client.get(f"/api/v1/stories/{story_id}").json()
    _assert_utc_iso(detail["created_at"])
    _assert_utc_iso(detail["updated_at"])

    assert detail["events"], "story creation records an event"
    _assert_utc_iso(detail["events"][0]["created_at"])

    note = client.post(
        f"/api/v1/stories/{story_id}/notes", json={"body": "watch the clock"}
    ).json()
    _assert_utc_iso(note["created_at"])

    detail = client.get(f"/api/v1/stories/{story_id}").json()
    _assert_utc_iso(detail["notes"][-1]["created_at"])

    listed = [s for s in client.get("/api/v1/stories").json() if s["id"] == story_id]
    assert len(listed) == 1
    _assert_utc_iso(listed[0]["created_at"])
    _assert_utc_iso(listed[0]["updated_at"])


def test_story_payload_sse_builder_carries_utc_offset(client):
    """`story_payload()` feeds bus.publish → SSE /stories/events."""
    story_id = _create_story(client, "ts sse")
    with Session(client.app.state.engine) as db:
        story = db.exec(select(Story).where(Story.id == story_id)).one()
        payload = story_payload(story)

    _assert_utc_iso(payload["created_at"])
    _assert_utc_iso(payload["updated_at"])
    assert payload["claimed_at"] is None


def test_bus_publication_reaches_sse_subscribers_with_offset(client):
    """End-to-end shape check of the SSE frame: subscribe via the real bus,
    create a story through the router, and confirm the published payload's
    timestamps carry the offset — byte-for-byte what
    `/api/v1/stories/events` streams to the board. `bus.publish` uses
    `put_nowait`, so a sync read of the queue suffices; frames are the exact
    JSON strings the SSE endpoint relays."""
    queue = asyncio.run(bus.subscribe())
    try:
        story_id = _create_story(client, "ts bus")
    finally:
        bus.unsubscribe(queue)

    frames = [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]
    published = [
        f["data"]["story"]
        for f in frames
        if f["kind"] == "story" and f["data"].get("story", {}).get("id") == story_id
    ]
    assert published, f"no story frame published for {story_id}: {frames}"
    payload = published[-1]
    _assert_utc_iso(payload["created_at"])
    _assert_utc_iso(payload["updated_at"])
    assert payload["claimed_at"] is None


def test_naive_datetimes_are_assumed_utc_not_shifted(client):
    """Existing rows stored naive must stay on the same wall clock."""
    story_id = _create_story(client, "ts naive")
    with Session(client.app.state.engine) as db:
        story = db.exec(select(Story).where(Story.id == story_id)).one()
        story.created_at = story.created_at.replace(tzinfo=None)
        naive = story.created_at
        db.add(story)
        db.commit()
        payload = story_payload(story)

    parsed = _assert_utc_iso(payload["created_at"])
    assert parsed.replace(tzinfo=None) == naive, "naive value must not shift"


def test_aware_datetimes_pass_through_untouched(client):
    story = Story(id="ts-aware", title="aware", created_by="test")
    story.created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    payload = story_payload(story)
    assert payload["created_at"] == "2026-01-02T03:04:05+00:00"
