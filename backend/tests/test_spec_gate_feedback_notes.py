"""Feedback notes posted at the spec gate reach the coder and the code review."""

from __future__ import annotations

from tests.test_coder_feedback_prompt import RecordingGW, _coder_prompts
from tests.test_run_verify_tool import (
    SPEC_OK,
    _tc_write,
    _wait_status,
)

DECISION = "use the blue palette, not the green one"
PLAIN = "plain remark that is not a decision"
OLD = "feedback from before the spec was written"
HEADING = "## Coordinator decisions (binding)"


def _run(tmp_path, monkeypatch, *, pre_spec_feedback=False):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers import Delta as D
    from tests.test_run_verify_tool import init_repo

    moves = [
        [D(SPEC_OK)],
        [D("VERDICT: APPROVED")],
        [_tc_write("a.py", "A = 1\n", "w1"), D("done")],
        [D("done")],
        [D("VERDICT: APPROVED")],
    ]
    gw = RecordingGW(moves)
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "gate.db"))
    client = TestClient(create_app(gateway=gw))
    client.__enter__()
    client.patch(
        "/api/v1/settings",
        json={"story_poll_interval": 0.05, "project_root": repo, "write_tools_enabled": True},
    )
    sid = client.post("/api/v1/stories", json={"title": "gate me"}).json()["id"]
    if pre_spec_feedback:
        client.post(f"/api/v1/stories/{sid}/notes", json={"body": OLD, "as_feedback": True})
    client.post(f"/api/v1/stories/{sid}/queue")
    _wait_status(client, sid, ("human_review", "failed"))
    r = client.post(f"/api/v1/stories/{sid}/notes", json={"body": DECISION, "as_feedback": True})
    note_id = r.json()["id"]
    client.post(f"/api/v1/stories/{sid}/notes", json={"body": PLAIN})
    client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
    final = _wait_status(client, sid, ("human_review", "failed"), timeout=40.0)
    assert final["status"] == "human_review", final.get("error")
    return client, sid, gw, note_id


def _review_prompt(gw) -> str:
    for messages in gw.messages:
        for m in messages:
            c = m.get("content")
            if m.get("role") == "user" and isinstance(c, str) \
                    and c.startswith("=== CODE REVIEW REQUEST"):
                return c
    raise AssertionError("no code review prompt")


def test_gate_feedback_reaches_coder_and_reviewer(tmp_path, monkeypatch):
    client, sid, gw, note_id = _run(tmp_path, monkeypatch)
    try:
        prompt = _coder_prompts(gw)[0]
        assert HEADING in prompt
        assert DECISION in prompt
        assert prompt.index(HEADING) > prompt.index("## Spec to implement")
        assert PLAIN not in prompt.split(HEADING)[1].split("\n## ")[0]
        review = _review_prompt(gw)
        assert HEADING in review and DECISION in review
        assert "Any decision not implemented in the diff means REJECTED." in review
        notes = {n["id"]: n for n in client.get(f"/api/v1/stories/{sid}").json()["notes"]}
        assert notes[note_id]["as_feedback"] is True
        assert notes[note_id]["seen_by_coder"] is True
    finally:
        client.__exit__(None, None, None)


def test_plain_and_pre_spec_notes_are_not_binding(tmp_path, monkeypatch):
    client, sid, gw, _ = _run(tmp_path, monkeypatch, pre_spec_feedback=True)
    try:
        section = _coder_prompts(gw)[0].split(HEADING)[1].split("\n## ")[0]
        assert OLD not in section
        assert PLAIN not in section
        assert DECISION in section
    finally:
        client.__exit__(None, None, None)
