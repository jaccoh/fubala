"""A coder rework run must see why its previous attempt was rejected.

Found 23 Oct 2026 on ems stories c6548d63 and 6c937c2e: three rework rounds
in a row ignored every rejection, because the prompt builder read the newest
note event AFTER recording the run's own worktree note in the same session --
so the newest note was always {"worktree": ...} and the reason never reached
the coder. These tests capture the coder's prompt on the rework round.
"""

from __future__ import annotations

import json

from tests.test_run_verify_tool import (
    SPEC_OK,
    ScriptedGW,
    _run_guard_story,
    _tc_write,
    _wait_status,
)

AI_REASON = "point 1 unaddressed: the stop still cancels one quarter"
HUMAN_REASON = "1. cancel the whole segment 2. keep it across a restart"


class RecordingGW(ScriptedGW):
    """ScriptedGW that keeps every call's messages, so a test can read the
    exact prompt each pipeline stage sent."""

    def __init__(self, moves):
        super().__init__(moves)
        self.messages: list[list[dict]] = []

    async def stream_chat(self, *, model, messages, tools=None, **kwargs):
        self.messages.append(messages)
        async for event in super().stream_chat(model=model, messages=messages,
                                               tools=tools, **kwargs):
            yield event


def _coder_prompts(gw: RecordingGW) -> list[str]:
    """The user prompt of every coder RUN, in call order. A run makes several
    gateway calls (tool round, closing round) over the same conversation, so
    consecutive calls carrying the same prompt are one run."""
    out = []
    for messages in gw.messages:
        for m in messages:
            content = m.get("content") if isinstance(m, dict) else None
            if m.get("role") == "user" and isinstance(content, str) \
                    and content.startswith("=== CODE REQUEST ==="):
                if not out or out[-1] != content:
                    out.append(content)
                break
    return out


def test_the_rework_prompt_carries_the_code_reviewers_rejection(tmp_path, monkeypatch):
    from app.providers import Delta as D

    moves = [
        [D(SPEC_OK)],                                   # spec writer
        [D("VERDICT: APPROVED")],                       # spec ai review
        [_tc_write("a.py", "A = 1\n", "w1"), D("done")],  # coder attempt 1
        [D("done")],
        [D(f"VERDICT: REJECTED - {AI_REASON}")],        # code review rejects
        [_tc_write("a.py", "A = 2\n", "w2"), D("done")],  # coder attempt 2
        [D("done")],
        [D("VERDICT: APPROVED")],                       # code review
    ]
    gw = RecordingGW(moves)
    client, sid = _run_guard_story(tmp_path, monkeypatch, gw)
    try:
        final = _wait_status(client, sid, ("human_review", "failed"))
        assert final["status"] == "human_review", final.get("error")
        prompts = _coder_prompts(gw)
        assert len(prompts) >= 2, prompts
        assert AI_REASON in prompts[-1], prompts[-1]
        assert "Previous attempt feedback" in prompts[-1]
    finally:
        client.__exit__(None, None, None)


def test_the_rework_prompt_carries_the_humans_code_gate_reasons(tmp_path, monkeypatch):
    """After a human rejects at the code gate, the next coder round sees the
    human's reason verbatim -- also when the AI review rejected again in
    between (its one-line reason must not hide the human's numbered list)."""
    from app.providers import Delta as D

    moves = [
        [D(SPEC_OK)],
        [D("VERDICT: APPROVED")],
        [_tc_write("a.py", "A = 1\n", "w1"), D("done")],  # coder attempt 1
        [D("done")],
        [D("VERDICT: APPROVED")],                       # code review -> human gate
        # human rejects; coder attempt 2
        [_tc_write("a.py", "A = 2\n", "w2"), D("done")],
        [D("done")],
        [D(f"VERDICT: REJECTED - {AI_REASON}")],        # AI review rejects again
        # coder attempt 3: must still see the human's reasons
        [_tc_write("a.py", "A = 3\n", "w3"), D("done")],
        [D("done")],
        [D("VERDICT: APPROVED")],
    ]
    gw = RecordingGW(moves)
    client, sid = _run_guard_story(tmp_path, monkeypatch, gw)
    try:
        first = _wait_status(client, sid, ("human_review", "failed"))
        assert first["status"] == "human_review", first.get("error")
        r = client.post(f"/api/v1/stories/{sid}/reject",
                        json={"reason": HUMAN_REASON, "by": "coordinator"})
        assert r.status_code == 200, r.text
        final = _wait_status(client, sid, ("human_review", "failed"), timeout=40.0)
        assert final["status"] == "human_review", final.get("error")
        prompts = _coder_prompts(gw)
        assert len(prompts) >= 3, [p[:80] for p in prompts]
        # the round right after the human reject
        assert HUMAN_REASON in prompts[1], prompts[1]
        # the round after the AI review rejected again: both reasons
        assert HUMAN_REASON in prompts[2], prompts[2]
        assert AI_REASON in prompts[2], prompts[2]
    finally:
        client.__exit__(None, None, None)


def test_a_worktree_note_is_never_read_as_feedback(tmp_path, monkeypatch):
    """The first coder round has no rejection behind it: no feedback block,
    and the run's own worktree note never shows up as one."""
    from app.providers import Delta as D

    moves = [
        [D(SPEC_OK)],
        [D("VERDICT: APPROVED")],
        [_tc_write("a.py", "A = 1\n", "w1"), D("done")],
        [D("done")],
        [D("VERDICT: APPROVED")],
    ]
    gw = RecordingGW(moves)
    client, sid = _run_guard_story(tmp_path, monkeypatch, gw)
    try:
        _wait_status(client, sid, ("human_review", "failed"))
        prompts = _coder_prompts(gw)
        assert prompts, gw.messages
        assert "Previous attempt feedback" not in prompts[0]
        assert "worktree" not in json.dumps(prompts[0]).lower() or \
            ".fubala-worktrees" not in prompts[0]
    finally:
        client.__exit__(None, None, None)
