"""The code review must answer every point of the last human rejection.

Incident: a human rejection carrying several numbered points ("1. … 2. …")
was treated as ONE reason, so a single `Reason 1: ADDRESSED` line satisfied
the override and an approving verdict shipped code with the other points
unanswered. These tests pin the per-point contract: every point is listed
separately, must be answered individually, and a hedged answer ("cannot
falsify", "assumption") or a silent point forces a rejection naming it.
Operator notes (POST /notes) written since spec approval reach the same
prompt. Reuses the test_debug_ac3 harness (spec -> approve -> coder ->
human gate -> reject -> rerun -> code review).
"""

import json

from fastapi.testclient import TestClient
from test_execution import (
    ReasonedRetryGW,
    _takein_story_to_review_gate,
    _takein_wait_terminal,
    init_repo,
)

from app.config import get_settings
from app.main import create_app

# one human reason carrying TWO numbered points, written inline — the shape
# the old single-reason check rubber-stamped. Point 1 names work the diff
# never did; point 2 names the file the harness's coder actually writes.
TWO_POINT_REASON = "1. add the cancel flag to cancel.py 2. set X = 1 in feature.py"
POINT_1 = "add the cancel flag to cancel.py"
POINT_2 = "set X = 1 in feature.py"


def _points_block(prompt: str) -> str:
    """The renumbered checklist section of the review prompt: everything
    between its heading and the reply contract, so a test can assert the
    points appear as separately numbered items (not only inside the verbatim
    quote of the human's reason)."""
    marker = "Points to check, one by one:"
    assert marker in prompt, prompt
    return prompt.split(marker, 1)[1].split("For each point above", 1)[0]


def _reason_notes(client, sid):
    """note-kind events written by the code reviewer, parsed."""
    detail = client.get(f"/api/v1/stories/{sid}").json()
    return [
        json.loads(e["data"])
        for e in detail.get("events") or []
        if e["kind"] == "note" and json.loads(e["data"]).get("by") == "code-reviewer"
    ]


class _PointsGW(ReasonedRetryGW):
    """ReasonedRetryGW whose code review answers the human's points.

    `first_reply` is what the FIRST post-reject review says; every later
    round answers every point cleanly with file:line evidence, so the story
    always terminates at the human gate.
    """

    def __init__(self, first_reply: str):
        super().__init__()
        self.first_reply = first_reply
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
                if self.reasoned_reviews == 1:
                    yield D(text=self.first_reply)
                else:
                    yield D(
                        text=(
                            "Reason 1: ADDRESSED — cancel.py:4\n"
                            "Reason 2: ADDRESSED — feature.py:1\n\n"
                            "VERDICT: APPROVED"
                        )
                    )
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


def _run_to_gate_and_reject(client, sid, reason):
    """Snapshot the prompt/event offsets, reject at the code gate and wait
    for the story to park somewhere a human must look."""
    pre_events = len(client.get(f"/api/v1/stories/{sid}").json()["events"])
    rejected = client.post(f"/api/v1/stories/{sid}/reject", json={"reason": reason})
    assert rejected.status_code == 200, rejected.text
    final = _takein_wait_terminal(client, sid, timeout=60)
    assert final["status"] == "human_review", final.get("error")
    detail = client.get(f"/api/v1/stories/{sid}").json()
    return detail, detail["events"][pre_events:]


def test_hedged_answer_on_one_point_forces_rejection(tmp_path, monkeypatch, legacy_review):
    """AC: the reviewer approves while hedging point 1 ('cannot falsify') and
    answering point 2 with real evidence — the hedge is not a verification,
    so the verdict is overridden to rejected and the note names Reason 1."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "crpts1.db"))

    gw = _PointsGW(
        "Reason 1: ADDRESSED — cannot falsify from this chunk\n"
        "Reason 2: ADDRESSED — a.py:5\n\n"
        "VERDICT: APPROVED"
    )
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(
            client, repo, "points hedge", verify_command=""
        )
        detail, after = _run_to_gate_and_reject(client, sid, TWO_POINT_REASON)

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
            n for n in _reason_notes(client, sid) if "Reason 1" in (n.get("reason") or "")
        ]
        assert naming, _reason_notes(client, sid)
        assert "cannot falsify" in naming[0]["reason"], naming[0]

        # the prompt carried the human's reason verbatim AND the split points
        prompt = next(
            p for p in gw.prompts if "## Human reject reasons to verify" in p
        )
        assert TWO_POINT_REASON in prompt, prompt
        assert "> 1. " in prompt  # quoted verbatim block
        block = _points_block(prompt)
        assert f"1. {POINT_1}" in block, block
        assert f"2. {POINT_2}" in block, block
        assert "Reason N: ADDRESSED" in prompt
        assert "cannot verify a point, it is NOT ADDRESSED" in prompt
        assert detail["ai_verdict"] == "approved"  # the recovering round


def test_silent_point_forces_rejection(tmp_path, monkeypatch, legacy_review):
    """AC variant: the reviewer answers point 2 only and stays silent on
    point 1 while approving — the missing line is an unanswered point, so
    the story is rejected with Reason 1 named."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "crpts2.db"))

    gw = _PointsGW(
        "Reason 2: ADDRESSED — a.py:5\n\n" "VERDICT: APPROVED"
    )
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(
            client, repo, "points silent", verify_command=""
        )
        detail, after = _run_to_gate_and_reject(client, sid, TWO_POINT_REASON)

        ai_verdicts = [
            json.loads(e["data"])["verdict"]
            for e in after
            if e["kind"] == "ai_review"
        ]
        assert "rejected" in ai_verdicts, ai_verdicts
        naming = [
            n
            for n in _reason_notes(client, sid)
            if (n.get("reason") or "").startswith("Reason 1")
        ]
        assert naming, _reason_notes(client, sid)
        assert POINT_1 in naming[0]["reason"], naming[0]


def test_operator_note_reaches_the_review_prompt(tmp_path, monkeypatch):
    """AC: a note posted through POST /notes after the spec was approved
    appears verbatim in the code-review prompt, with its author labelled."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "crpts3.db"))
    note_body = "also export CANCELLED from the package __init__"

    gw = _PointsGW("Reason 1: ADDRESSED — cancel.py:4\n\nVERDICT: APPROVED")
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(
            client, repo, "operator note", verify_command=""
        )
        posted = client.post(
            f"/api/v1/stories/{sid}/notes", json={"body": note_body, "author": "op"}
        )
        assert posted.status_code == 201, posted.text
        _run_to_gate_and_reject(client, sid, "1. add the cancel flag to cancel.py")

        prompt = next(
            p for p in gw.prompts if "## Human reject reasons to verify" in p
        )
        assert "## Operator notes since spec approval" in prompt, prompt
        assert note_body in prompt, prompt
        assert "(from op):" in prompt, prompt


def test_every_point_answered_reaches_human_review(tmp_path, monkeypatch, legacy_review):
    """AC: a multi-point rejection answered point by point, each with
    file:line evidence and no hedging, plus an approving verdict, reaches
    the human gate without a rejection round in between."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "crpts4.db"))

    gw = _PointsGW(
        "Reason 1: ADDRESSED — cancel.py:4\n"
        "Reason 2: ADDRESSED — feature.py:1\n\n"
        "VERDICT: APPROVED"
    )
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(
            client, repo, "points clean", verify_command=""
        )
        detail, after = _run_to_gate_and_reject(client, sid, TWO_POINT_REASON)

        ai_verdicts = [
            json.loads(e["data"])["verdict"]
            for e in after
            if e["kind"] == "ai_review"
        ]
        assert ai_verdicts == ["approved"], ai_verdicts
        assert detail["status"] == "human_review"
        assert not [
            n for n in _reason_notes(client, sid) if n.get("reason", "").startswith("Reason")
        ], _reason_notes(client, sid)


# ---- unit level: the split and the override ------------------------------


def test_two_consecutive_rejections_park_instead_of_requeueing(
    tmp_path, monkeypatch, legacy_review):
    """Loop guard: after CODE_REVIEW_REJECT_LIMIT consecutive AI code-review
    rejections since the human's last touch, the next one parks the story at
    the human gate naming the unanswered point instead of requeueing the
    coder into the same disagreement."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "crpts5.db"))

    class AlwaysRejectsGW(ReasonedRetryGW):
        """Every post-reject review approves the verdict line but leaves
        point 1 unaddressed — the override rejects, forever."""

        def __init__(self):
            super().__init__()
            self.reasoned_reviews = 0

        async def stream_chat(
            self, *, model, messages, tools=None, max_output_tokens=None
        ):
            from app.providers import Delta as D
            from app.providers import Usage as U

            prompt = (messages[-1].get("content") or "")
            if prompt.startswith("=== CODE REVIEW REQUEST"):
                self.calls += 1
                self.prompts.append(prompt)
                if "## Human reject reasons to verify" in prompt:
                    self.reasoned_reviews += 1
                    yield D(
                        text=(
                            "Reason 1: ADDRESSED — cannot falsify from this chunk\n"
                            "Reason 2: ADDRESSED — a.py:5\n\n"
                            "VERDICT: APPROVED"
                        )
                    )
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

    gw = AlwaysRejectsGW()
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(
            client, repo, "park loop", verify_command=""
        )
        detail, after = _run_to_gate_and_reject(client, sid, TWO_POINT_REASON)

        # parked, not requeued: the third rejection never requeues the coder
        assert detail["status"] == "human_review"
        requeued = [
            json.loads(e["data"])
            for e in after
            if e["kind"] == "status" and json.loads(e["data"]).get("to") == "queued"
        ]
        assert requeued, "the first rejections should requeue"
        park_notes = [
            n
            for n in _reason_notes(client, sid)
            if (n.get("reason") or "").startswith("code review rejected")
        ]
        assert park_notes, _reason_notes(client, sid)
        assert "Reason 1" in park_notes[0]["reason"], park_notes[0]
        assert detail.get("error", "").startswith("code review rejected"), detail.get(
            "error"
        )


def test_reason_points_split():
    from app.runner import _reason_points

    assert _reason_points(["1. X 2. Y"]) == ["X", "Y"]
    assert _reason_points(["plain objection"]) == ["plain objection"]
    assert _reason_points(["1. first\nwrapped continuation\n2. second"]) == [
        "first\nwrapped continuation",
        "second",
    ]
    assert _reason_points([]) == []
    # a numbered point alone keeps its text, loses its own number
    assert _reason_points(["3. the latch"]) == ["the latch"]


def test_unaddressed_reason_doubt_beats_addressed():
    """The override's contract: honest ADDRESSED answers carrying file:line
    evidence pass even when their prose contains hedge-adjacent words ("no
    evidence", "assumption"); ADDRESSED without a citation rejects, naming
    the point and the missing evidence; genuine first-person doubt
    ("cannot falsify", "assumed", "not verified") rejects even WITH a
    citation — doubt beats ADDRESSED and outranks evidence; NOT ADDRESSED
    and the no-Reason-lines fail-open are unchanged."""
    from app.runner import _unaddressed_reason

    points = ["X", "Y"]
    assert _unaddressed_reason(
        "Reason 1: ADDRESSED — a.py:1\nReason 2: ADDRESSED — b.py:2", points
    ) is None
    # descriptive prose + evidence is not doubt: both honest examples pass
    assert _unaddressed_reason(
        "Reason 1: ADDRESSED - no evidence of night_consumed remains, "
        "app.py:412\n"
        "Reason 2: ADDRESSED — assumption documented in tests/x.py:20",
        points,
    ) is None
    # ADDRESSED without file:line: rejected, naming point + missing evidence
    missing = _unaddressed_reason(
        "Reason 1: ADDRESSED\nReason 2: ADDRESSED — b.py:2", points
    ) or ""
    assert "Reason 1" in missing, missing
    assert "evidence" in missing, missing
    assert "X" in missing, missing
    # doubt still beats ADDRESSED — even with a citation attached
    assert "Reason 1" in (_unaddressed_reason(
        "Reason 1: ADDRESSED - cannot falsify, x.py:3\n"
        "Reason 2: ADDRESSED — b.py:2",
        points,
    ) or "")
    for hedge in ("assumed", "not verified"):
        assert "Reason 1" in (_unaddressed_reason(
            f"Reason 1: ADDRESSED — {hedge}, a.py:1\n"
            "Reason 2: ADDRESSED — b.py:2",
            points,
        ) or "")
    assert "NOT ADDRESSED" in (_unaddressed_reason(
        "Reason 1: NOT ADDRESSED — no file touches it\nReason 2: ADDRESSED — b.py:2",
        points,
    ) or "")
    # no per-reason lines at all: fail-open, the verdict decides
    assert _unaddressed_reason("looks fine\nVERDICT: APPROVED", points) is None


def test_evidence_must_look_like_a_path():
    """AC (coordinator follow-up): the file:line citation must look like a
    path — a name with an extension before the colon, or a known
    extensionless file (Dockerfile, Makefile). Clock times ("12:45") and
    ratios ("3:1") are prose reviewers write constantly; counting them as
    citations would let an unevidenced answer pass the gate."""
    from app.runner import _unaddressed_reason

    points = ["X", "Y"]
    # not evidence: clock time, ratio, bare host:port
    for prose in (
        "the window 12:45 now opens",
        "ratio 3:1",
        "host:8080 serves it",
    ):
        rejection = _unaddressed_reason(
            f"Reason 1: ADDRESSED - {prose}\nReason 2: ADDRESSED — b.py:2",
            points,
        ) or ""
        assert "Reason 1" in rejection, (prose, rejection)
        assert "evidence" in rejection, (prose, rejection)
    # evidence: dotted path anywhere in the text, and extensionless Dockerfile
    for answer in (
        "Reason 1: ADDRESSED — src/ems/app.py:412\n"
        "Reason 2: ADDRESSED — b.py:2",
        "Reason 1: ADDRESSED - image rebuilt, see Dockerfile:12\n"
        "Reason 2: ADDRESSED — b.py:2",
    ):
        assert _unaddressed_reason(answer, points) is None, answer


def test_pipeline_note_authors_are_excluded_from_operator_notes(
    tmp_path, monkeypatch
):
    """Reviewer feedback: `_operator_notes_since` must exclude the pipeline's
    own StoryNote authors (AGENT_AUTHORS ∪ fubala), so a fubala run summary
    or a code-reviewer diagnostic can never reach the code-review prompt as
    an operator requirement. The union is structural, not incidental: it
    holds even if AGENT_AUTHORS is narrowed elsewhere."""
    from app.runner import _is_pipeline_note_author, _operator_notes_since

    for author in ("fubala", "code-reviewer", "vendored-guard", "scheduler"):
        assert _is_pipeline_note_author(author), author
    assert not _is_pipeline_note_author("op")
    assert not _is_pipeline_note_author(None)
    assert not _is_pipeline_note_author("")

    # and through the query: pipeline notes posted after spec approval are
    # filtered out while an operator-authored note posted alongside is
    # returned — the positive control proves the assertion passes for the
    # right reason (a working filter, not a query that finds nothing). The
    # harness writes straight into the DB beside the operator's own note
    # path (POST /notes is covered by the prompt test). The insert session
    # commits on exit, so the later query — in a FRESH session — actually
    # sees the rows (the nested-session version never did: it queried its
    # own uncommitted snapshot and passed for the wrong reason).
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "pts.db"))
    from fastapi.testclient import TestClient
    from test_execution import ReasonedRetryGW, init_repo

    repo = init_repo(tmp_path)
    gw = ReasonedRetryGW()
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(
            client, repo, "pipeline notes", verify_command=""
        )
        from app.db import make_engine, session_scope
        from app.models import StoryNote

        engine = make_engine(get_settings())
        with session_scope(engine) as db:
            db.add(
                StoryNote(
                    story_id=sid,
                    body="artefact guard kept node_modules out of the diff",
                    author="fubala",
                )
            )
            db.add(
                StoryNote(
                    story_id=sid,
                    body="also export CANCELLED from __init__",
                    author="code-reviewer",
                )
            )
            # positive control: inserted after spec approval, operator-authored
            db.add(
                StoryNote(
                    story_id=sid,
                    body="keep the cancel flag optional",
                    author="op",
                )
            )
        with session_scope(engine) as db:
            notes = _operator_notes_since(db, sid)
            assert [(author, body) for _, author, body in notes] == [
                ("op", "keep the cancel flag optional")
            ], notes

