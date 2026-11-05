"""The coder prompt carries the operator's notes written since the newest
spec approval -- verbatim, author- and time-labelled.

Story 856ff62a, 24 Oct 2026: steering a running story required a reject,
which resets the pipeline; the coder prompt now renders POST /notes written
after the newest `spec_approved` as primary steering (same selection query
the code-review prompt uses, `_operator_notes_since`). These tests capture
the coder's prompt through the same RecordingGW the rework-feedback tests
use, so what a coder round actually saw is asserted, not what a helper
returned.
"""

from __future__ import annotations

from app.runner import (
    _CODER_NOTES_CAP,
    AGENT_AUTHORS,
    _coder_operator_notes_section,
)
from tests.test_coder_feedback_prompt import RecordingGW, _coder_prompts
from tests.test_run_verify_tool import (
    SPEC_OK,
    _run_guard_story,
    _tc_write,
    _wait_status,
)

SECTION = "## Operator notes since the spec approval (follow them)"
NOTE_BODY = "also export CANCELLED from the package __init__"


def _post_note(client, sid: str, body: str, author: str) -> dict:
    r = client.post(
        f"/api/v1/stories/{sid}/notes", json={"body": body, "author": author}
    )
    assert r.status_code == 201, r.text
    return r.json()


def _steer_flow_moves():
    """Spec -> ai review -> coder attempt 1 -> code review approves -> human
    gate -> coder attempt 2 -> code review approves. Whatever the operator
    posts while the story waits at the first gate is steering for attempt 2;
    attempt 2's round is reached through a code-gate reject (the only route
    back to the coder once the review has approved)."""
    from app.providers import Delta as D

    return [
        [D(SPEC_OK)],  # spec writer
        [D("VERDICT: APPROVED")],  # spec ai review
        [_tc_write("a.py", "A = 1\n", "w1"), D("done")],  # coder attempt 1
        [D("done")],
        [D("VERDICT: APPROVED")],  # code review -> human gate
        [_tc_write("a.py", "A = 2\n", "w2"), D("done")],  # coder attempt 2
        [D("done")],
        [D("VERDICT: APPROVED")],
    ]


def test_note_after_spec_approval_reaches_the_coder_prompt(tmp_path, monkeypatch):
    """A note posted through POST /notes after the spec was approved appears
    verbatim in the next coder prompt, author- and time-labelled, as primary
    steering: after the spec, before the rework feedback."""
    gw = RecordingGW(_steer_flow_moves())
    client, sid = _run_guard_story(tmp_path, monkeypatch, gw)
    try:
        first = _wait_status(client, sid, ("human_review", "failed"))
        assert first["status"] == "human_review", first.get("error")
        posted = _post_note(client, sid, NOTE_BODY, "op")
        r = client.post(
            f"/api/v1/stories/{sid}/reject",
            json={"reason": "apply the note", "by": "coordinator"},
        )
        assert r.status_code == 200, r.text
        final = _wait_status(client, sid, ("human_review", "failed"))
        assert final["status"] == "human_review", final.get("error")

        prompts = _coder_prompts(gw)
        assert prompts, "no coder prompt captured"
        prompt = prompts[-1]
        assert SECTION in prompt, prompt
        expected = f"- [{posted['created_at']}] (from op): {NOTE_BODY}"
        assert expected in prompt, prompt
        # primary steering: after the spec, before the rework feedback
        assert prompt.index("## Spec to implement") < prompt.index(SECTION)
        assert prompt.index(SECTION) < prompt.index("## Previous attempt feedback")
    finally:
        client.__exit__(None, None, None)


def test_note_from_before_the_approval_stays_out(tmp_path, monkeypatch):
    """A note written BEFORE the spec was approved predates the story's
    execution start: it never qualifies as post-approval steering, even on
    the retry round."""
    from datetime import timedelta

    from app.config import get_settings
    from app.db import make_engine, session_scope
    from app.models import StoryNote, utcnow

    gw = RecordingGW(_steer_flow_moves())
    client, sid = _run_guard_story(tmp_path, monkeypatch, gw)
    try:
        first = _wait_status(client, sid, ("human_review", "failed"))
        assert first["status"] == "human_review", first.get("error")
        engine = make_engine(get_settings())
        with session_scope(engine) as db:
            db.add(
                StoryNote(
                    story_id=sid,
                    body="pre-approval context that must not steer the coder",
                    author="op",
                    # written an hour before the approval happened
                    created_at=utcnow() - timedelta(hours=1),
                )
            )
        r = client.post(
            f"/api/v1/stories/{sid}/reject",
            json={"reason": "apply the note", "by": "coordinator"},
        )
        assert r.status_code == 200, r.text
        final = _wait_status(client, sid, ("human_review", "failed"))
        assert final["status"] == "human_review", final.get("error")

        prompts = _coder_prompts(gw)
        assert prompts
        assert SECTION not in prompts[-1], prompts[-1]
        assert "pre-approval context" not in prompts[-1], prompts[-1]
    finally:
        client.__exit__(None, None, None)


def test_pipeline_authored_note_stays_out(tmp_path, monkeypatch):
    """The pipeline's own diagnostics (AGENT_AUTHORS and fubala) never steer
    the coder: only operator-authored notes qualify."""
    from app.config import get_settings
    from app.db import make_engine, session_scope
    from app.models import StoryNote

    moves = _steer_flow_moves()
    gw = RecordingGW(moves)
    client, sid = _run_guard_story(tmp_path, monkeypatch, gw)
    try:
        first = _wait_status(client, sid, ("human_review", "failed"))
        assert first["status"] == "human_review", first.get("error")
        engine = make_engine(get_settings())
        with session_scope(engine) as db:
            for author in (*AGENT_AUTHORS, "fubala"):
                db.add(
                    StoryNote(
                        story_id=sid,
                        body=f"diagnostic written by {author}",
                        author=author,
                    )
                )
        r = client.post(
            f"/api/v1/stories/{sid}/reject",
            json={"reason": "apply the note", "by": "coordinator"},
        )
        assert r.status_code == 200, r.text
        final = _wait_status(client, sid, ("human_review", "failed"))
        assert final["status"] == "human_review", final.get("error")

        prompts = _coder_prompts(gw)
        assert prompts
        assert SECTION not in prompts[-1], prompts[-1]
        assert "diagnostic written by" not in prompts[-1], prompts[-1]
    finally:
        client.__exit__(None, None, None)


def test_no_qualifying_notes_renders_no_section(tmp_path, monkeypatch):
    """No operator notes at all: the section is absent entirely -- no empty
    header for the coder to trip over."""
    gw = RecordingGW(_steer_flow_moves())
    client, sid = _run_guard_story(tmp_path, monkeypatch, gw)
    try:
        first = _wait_status(client, sid, ("human_review", "failed"))
        assert first["status"] == "human_review", first.get("error")
        r = client.post(
            f"/api/v1/stories/{sid}/reject",
            json={"reason": "apply the note", "by": "coordinator"},
        )
        assert r.status_code == 200, r.text
        final = _wait_status(client, sid, ("human_review", "failed"))
        assert final["status"] == "human_review", final.get("error")

        prompts = _coder_prompts(gw)
        assert prompts
        assert SECTION not in prompts[-1], prompts[-1]
    finally:
        client.__exit__(None, None, None)


def test_cap_keeps_newest_notes_and_marks_the_cut():
    """Over the 8000-char cap the NEWEST notes survive, the oldest are cut,
    and a one-line omission marker sits at the top of the section; the
    rendered section never exceeds the cap."""
    iso = "2026-10-24T10:00:00+00:00"
    body = "x" * 90  # entry ~ 140 chars with label -> ~55 notes per cap
    notes = [(iso, f"op{i}", f"note {i}: {body}") for i in range(120)]
    oldest_body, newest_body = notes[0][2], notes[-1][2]

    section = _coder_operator_notes_section(notes)

    assert len(section) <= _CODER_NOTES_CAP
    assert "(older notes omitted)" in section
    assert newest_body in section
    assert oldest_body not in section
    # entries render oldest first among those kept -> the newest one last
    entries = [ln for ln in section.splitlines() if ln.startswith("- [")]
    assert entries, section
    assert entries[-1].endswith(newest_body)


def test_cap_marker_only_when_something_was_dropped():
    """Under the cap nothing is omitted: no marker line, oldest first."""
    iso = "2026-10-24T10:00:00+00:00"
    notes = [(iso, "op", f"note {i}") for i in range(3)]

    section = _coder_operator_notes_section(notes)

    assert "(older notes omitted)" not in section
    entries = [ln for ln in section.splitlines() if ln.startswith("- [")]
    assert [e.split(": ", 1)[1] for e in entries] == [
        "note 0",
        "note 1",
        "note 2",
    ]


def test_second_spec_approval_shifts_the_cutoff(tmp_path, monkeypatch):
    """The cut-off is the NEWEST spec_approved marker: notes written before a
    re-approval (e.g. a spec retry round) do not steer the coder, notes
    after it do. A second approval cannot come through POST /approve for a
    code-gate story (that route merges), so the marker is recorded the way
    the respec flow does."""
    from datetime import timedelta

    from app.config import get_settings
    from app.db import make_engine, session_scope
    from app.models import StoryNote, utcnow
    from app.runner import record_event

    gw = RecordingGW(_steer_flow_moves())
    client, sid = _run_guard_story(tmp_path, monkeypatch, gw)
    try:
        first = _wait_status(client, sid, ("human_review", "failed"))
        assert first["status"] == "human_review", first.get("error")
        engine = make_engine(get_settings())
        now = utcnow()
        with session_scope(engine) as db:
            db.add(
                StoryNote(
                    story_id=sid,
                    body="stale steering from before the re-approval",
                    author="op",
                    created_at=now - timedelta(minutes=5),
                )
            )
            record_event(db, sid, "spec_approved", {"by": "t"})
            db.add(
                StoryNote(
                    story_id=sid,
                    body="fresh steering written after the re-approval",
                    author="op",
                    created_at=now + timedelta(minutes=5),
                )
            )
        r = client.post(
            f"/api/v1/stories/{sid}/reject",
            json={"reason": "apply the note", "by": "coordinator"},
        )
        assert r.status_code == 200, r.text
        final = _wait_status(client, sid, ("human_review", "failed"))
        assert final["status"] == "human_review", final.get("error")

        prompts = _coder_prompts(gw)
        assert prompts
        prompt = prompts[-1]
        assert SECTION in prompt, prompt
        assert "fresh steering written after the re-approval" in prompt, prompt
        assert "stale steering from before the re-approval" not in prompt, prompt
    finally:
        client.__exit__(None, None, None)
