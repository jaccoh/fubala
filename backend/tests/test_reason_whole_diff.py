"""The AI code review judges each human reject reason against the WHOLE
diff, never against one chunk of it.

Incident (merge train 50a9f97c): a rework whose fix lived in an earlier
chunk was marked "not in this chunk" → NOT ADDRESSED, so a correct rework
kept bouncing back to the coder and large reworks parked at the human gate.
The reasons section now rides in ONE dedicated call that carries the full
uncapped diff (multi-chunk diffs only; the single-chunk prompt already has
the whole diff), and a point that only frames the others answers
'framing — see reasons n-m' with no file:line required.

Reuses the test_execution harness (spec -> approve -> coder -> human gate
-> reject -> rerun -> code review); the two-chunk shape follows
test_reviewer_diff_cap.py.
"""

import json

from fastapi.testclient import TestClient
from test_execution import (
    ReasonedRetryGW,
    _takein_story_to_review_gate,
    _takein_wait_terminal,
    init_repo,
)

from app.main import create_app

# written into big.py, whose diff alone exceeds the patched 2000-char chunk
# cap, so big.py opens chunk 1 — anything written after it lands in chunk 2
BIG_MARKER = "BIG_FILE_MARKER_5DF7"
CANCEL_MARKER = "CANCELLED = True"
REASON = "add the cancel flag to cancel.py"
REASON_HEAD = "add the cancel flag to cancel.py"


class _ChunkedReasonGW(ReasonedRetryGW):
    """Marker-routed gateway for a two-chunk rework diff.

    Round 1's coder writes big.py (~2.4k chars); the post-reject rerun adds
    cancel.py, the change the human asked for. The rerun's diff therefore
    splits into two chunks with cancel.py in chunk 2, and the reason is
    judged by the dedicated whole-diff call (`=== CODE REASONS JUDGEMENT`)
    — never by a chunk.

    `judgement_replies` is consumed in order by the reasons call; the chunk
    reviews always approve and never carry Reason lines.
    """

    def __init__(self, judgement_replies: list[str]):
        super().__init__()
        self.judgement_replies = list(judgement_replies)
        self.judgements = 0

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
        from app.providers import Delta as D
        from app.providers import ToolCall as TC
        from app.providers import Usage as U

        prompt = (messages[-1].get("content") or "")
        if prompt.startswith("=== CODE REQUEST ==="):
            self.calls += 1
            self.prompts.append(prompt)
            seen = sum(1 for p in self.prompts if p.startswith("=== CODE REQUEST ==="))
            if seen == 1:
                # round 1: only the big file -> the first review is already
                # chunked, and nothing addresses the human's reason yet
                yield TC(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps(
                        {
                            "path": "big.py",
                            "content": (
                                f"# big file\nMARKER = '{BIG_MARKER}'\n"
                                + "VALUE = 1\n" * 320
                            ),
                        }
                    ),
                )
            else:
                # the rerun adds the change the human asked for; git orders
                # big.py before cancel.py, so cancel.py sits in chunk 2
                yield TC(
                    id="w2",
                    name="write_file",
                    arguments=json.dumps(
                        {
                            "path": "cancel.py",
                            "content": f"{CANCEL_MARKER}  # the cancel flag\n",
                        }
                    ),
                )
            yield U(prompt_tokens=1, completion_tokens=1)
            return
        if prompt.startswith("=== CODE REASONS JUDGEMENT ==="):
            self.calls += 1
            self.prompts.append(prompt)
            self.judgements += 1
            reply = self.judgement_replies.pop(0) if self.judgement_replies else (
                "Reason 1: ADDRESSED — cancel.py:1"
            )
            yield D(text=reply)
            yield U(prompt_tokens=1, completion_tokens=1)
            return
        if prompt.startswith("=== CODE REVIEW REQUEST"):
            self.calls += 1
            self.prompts.append(prompt)
            # a chunk never judges the human's points any more: plain
            # approving chunk reviews with no Reason lines at all
            yield D(text="Chunk reviewed: clean.\n\nVERDICT: APPROVED")
            yield U(prompt_tokens=1, completion_tokens=1)
            return
        async for event in super().stream_chat(
            model=model,
            messages=messages,
            tools=tools,
            max_output_tokens=max_output_tokens,
        ):
            yield event


def _reason_notes(client, sid):
    """note-kind events written by the code reviewer, parsed."""
    detail = client.get(f"/api/v1/stories/{sid}").json()
    return [
        json.loads(e["data"])
        for e in detail.get("events") or []
        if e["kind"] == "note" and json.loads(e["data"]).get("by") == "code-reviewer"
    ]


def test_reason_fixed_in_later_chunk_is_judged_against_the_whole_diff(
    tmp_path, monkeypatch
):
    """AC: two-chunk rework diff, the fix lives in chunk 2. The reason is
    judged ADDRESSED with the chunk-2 file:line by ONE dedicated call whose
    prompt carries the whole diff (chunk-1 AND chunk-2 content); no chunk
    prompt carries the reasons section; the story reaches the human gate
    approved."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "rwd1.db"))
    monkeypatch.setattr("app.runner.DIFF_CHUNK_SIZE", 2000)

    gw = _ChunkedReasonGW(["Reason 1: ADDRESSED — cancel.py:1"])
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(
            client, repo, "whole diff reason", verify_command=""
        )
        pre_prompts = len(gw.prompts)
        client.post(f"/api/v1/stories/{sid}/reject", json={"reason": REASON})

        final = _takein_wait_terminal(client, sid, timeout=60)
        assert final["status"] == "human_review", final.get("error")
        assert final["ai_verdict"] == "approved", final.get("error")
        assert not _reason_notes(client, sid), _reason_notes(client, sid)

        # the rework really was chunked: chunk prompts were sent after the
        # reject, and cancel.py reached none of them alone
        after = gw.prompts[pre_prompts:]
        chunk_prompts = [p for p in after if p.startswith("=== CODE REVIEW REQUEST")]
        assert len(chunk_prompts) >= 2, [p[:80] for p in after]

        # exactly ONE prompt carries the reasons section: the dedicated
        # judgement call, and it carries the WHOLE diff — chunk-1 content
        # (big.py's marker) AND chunk-2 content (cancel.py)
        with_reasons = [
            p for p in after if "## Human reject reasons to verify" in p
        ]
        assert len(with_reasons) == 1, [p[:80] for p in with_reasons]
        judgement = with_reasons[0]
        assert judgement.startswith("=== CODE REASONS JUDGEMENT ==="), judgement[:120]
        assert BIG_MARKER in judgement, "chunk-1 content missing from the judgement"
        assert CANCEL_MARKER in judgement, "chunk-2 content missing from the judgement"
        assert REASON_HEAD in judgement
        assert "Points to check, one by one:" in judgement

        # and no chunk prompt judged the reasons: a chunk sees one slice
        # only, which is exactly the blindness this story removes
        assert all("## Human reject reasons to verify" not in p for p in chunk_prompts)

        # chunk 1 really does not carry the fix: the whole-diff view is the
        # only place the reviewer could see it
        assert CANCEL_MARKER not in chunk_prompts[0]

        # the verdict came from the chunk assembly, the judgement is stored
        # under its own heading, and its Reason line is what the override
        # accepted
        assert gw.judgements == 1
        assert "## Human reject reasons — judged against the whole diff" in (
            final["ai_review"] or ""
        )
        assert "Reason 1: ADDRESSED — cancel.py:1" in (final["ai_review"] or "")
        assert "VERDICT: APPROVED" in (final["ai_review"] or "")


def test_reason_absent_from_every_chunk_is_not_addressed(tmp_path, monkeypatch):
    """AC: no chunk carries the change — the whole-diff judgement says NOT
    ADDRESSED, the override rejects the approving verdict, the story is
    requeued and the note names the reason."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "rwd2.db"))
    monkeypatch.setattr("app.runner.DIFF_CHUNK_SIZE", 2000)

    gw = _ChunkedReasonGW(
        [
            "Reason 1: NOT ADDRESSED — no part of the diff adds the cancel "
            "flag to cancel.py",
            "Reason 1: ADDRESSED — cancel.py:1",
        ]
    )
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(
            client, repo, "absent reason", verify_command=""
        )
        pre_events = len(client.get(f"/api/v1/stories/{sid}").json()["events"])
        client.post(f"/api/v1/stories/{sid}/reject", json={"reason": REASON})

        final = _takein_wait_terminal(client, sid, timeout=60)
        assert final["status"] == "human_review", final.get("error")

        detail = client.get(f"/api/v1/stories/{sid}").json()
        after = detail["events"][pre_events:]
        ai_verdicts = [
            json.loads(e["data"])["verdict"]
            for e in after
            if e["kind"] == "ai_review"
        ]
        assert "rejected" in ai_verdicts, ai_verdicts
        requeued = [
            json.loads(e["data"])
            for e in after
            if e["kind"] == "status" and json.loads(e["data"]).get("to") == "queued"
        ]
        assert any(r.get("from") == "ai_review" for r in requeued), requeued

        naming = [
            n
            for n in _reason_notes(client, sid)
            if (n.get("reason") or "").startswith("Reason 1 NOT ADDRESSED")
        ]
        assert naming, _reason_notes(client, sid)
        assert "cancel" in naming[0]["reason"], naming[0]
        assert REASON_HEAD in naming[0]["reason"], naming[0]


# ---- framing --------------------------------------------------------------


class _FramingGW(ReasonedRetryGW):
    """Answers a two-point rejection whose first point only frames the
    second."""

    def __init__(self, reply: str):
        super().__init__()
        self.reply = reply
        self.reasoned_reviews = 0

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
        from app.providers import Delta as D
        from app.providers import Usage as U

        prompt = (messages[-1].get("content") or "")
        if prompt.startswith("=== CODE REVIEW REQUEST"):
            self.calls += 1
            self.prompts.append(prompt)
            if "## Human reject reasons to verify" in prompt:
                self.reasoned_reviews += 1
                yield D(text=self.reply)
            else:
                yield D(text="VERDICT: APPROVED")
            yield U(prompt_tokens=1, completion_tokens=1)
            return
        async for event in super().stream_chat(
            model=model,
            messages=messages,
            tools=tools,
            max_output_tokens=max_output_tokens,
        ):
            yield event


FRAMING_REASON = (
    "1. the retry contract: this point only frames the next one "
    "2. set X = 1 in feature.py"
)


def test_framing_answer_needs_no_evidence():
    """AC (unit): an answer starting with 'framing' passes the override
    without file:line evidence — a framing point has no change of its own
    to cite — while a non-framing unevidenced answer still rejects."""
    from app.runner import _unaddressed_reason

    points = ["the retry contract framing", "set X = 1 in feature.py"]
    assert (
        _unaddressed_reason(
            "Reason 1: framing — see reasons 2\n"
            "Reason 2: ADDRESSED — feature.py:1",
            points,
        )
        is None
    )
    # case-insensitive, and framing prose may carry doubt-adjacent words
    assert (
        _unaddressed_reason(
            "Reason 1: Framing — assumed scope, see reason 2\n"
            "Reason 2: ADDRESSED — feature.py:1",
            points,
        )
        is None
    )
    # a non-framing answer still needs evidence
    rejection = _unaddressed_reason(
        "Reason 1: ADDRESSED — covered by the framing above\n"
        "Reason 2: ADDRESSED — feature.py:1",
        points,
    )
    assert rejection is not None and "Reason 1" in rejection, rejection


def test_framing_point_reaches_the_human_gate(tmp_path, monkeypatch):
    """AC (pipeline): a reviewer who answers the framing point with
    'framing — see reason 2' and the real point with file:line evidence is
    not rejected for the missing citation on the framing point."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "rwd3.db"))

    gw = _FramingGW(
        "Reason 1: framing — see reason 2\n"
        "Reason 2: ADDRESSED — feature.py:1\n\n"
        "VERDICT: APPROVED"
    )
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(
            client, repo, "framing point", verify_command=""
        )
        pre_events = len(client.get(f"/api/v1/stories/{sid}").json()["events"])
        client.post(f"/api/v1/stories/{sid}/reject", json={"reason": FRAMING_REASON})

        final = _takein_wait_terminal(client, sid, timeout=60)
        assert final["status"] == "human_review", final.get("error")
        assert final["ai_verdict"] == "approved", final.get("error")

        detail = client.get(f"/api/v1/stories/{sid}").json()
        after = detail["events"][pre_events:]
        assert [
            json.loads(e["data"])["verdict"]
            for e in after
            if e["kind"] == "ai_review"
        ] == ["approved"]
        assert not [
            n
            for n in _reason_notes(client, sid)
            if (n.get("reason") or "").startswith("Reason")
        ], _reason_notes(client, sid)
