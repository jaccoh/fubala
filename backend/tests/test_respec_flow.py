"""Bounded reject/re-spec loop: parking at the human gate after N consecutive
AI-review rejections, labelled feedback, revise-not-restart prompts, reviewer
accountability, and spec-verdict history."""

import json
import time
from contextlib import contextmanager

import pytest


def create_story(client, **body) -> dict:
    res = client.post("/api/v1/stories", json={"title": "Add login", **body})
    assert res.status_code == 201
    return res.json()


def wait_for_status(client, story_id: str, want, timeout: float = 30.0) -> dict:
    want = (want,) if isinstance(want, str) else tuple(want)
    deadline = time.time() + timeout
    story = None
    while time.time() < deadline:
        story = client.get(f"/api/v1/stories/{story_id}").json()
        if story["status"] in want:
            return story
        time.sleep(0.05)
    pytest.fail(f"story never reached {want}; last status: {story and story['status']}")


class ScriptedGateway:
    """Canned gateway: spec requests get a scripted spec, review requests get
    review_fn(prompt). Every call is recorded in .calls."""

    def __init__(self, review_fn):
        self.review_fn = review_fn
        self.calls: list[dict] = []

    async def stream_chat(
        self, *, model: str, messages: list[dict], tools=None, max_output_tokens=None
    ):
        from app.providers import Delta, Usage

        from conftest import MINIMAL_SPEC

        self.calls.append({"model": model, "messages": messages, "tools": tools})
        user = messages[1]["content"]
        if user.startswith("=== SPEC REQUEST ==="):
            # must satisfy the runner's REQUIRED_SPEC_SECTIONS validation and
            # the deterministic preflight (last line must be terminal), so the
            # round marker is a list item, never a bare trailing line
            yield Delta(text=MINIMAL_SPEC + f"\n- round {len(self.calls)}\n")
            yield Usage(prompt_tokens=1, completion_tokens=1)
            return
        yield Delta(text=self.review_fn(user))
        yield Usage(prompt_tokens=1, completion_tokens=1)


@contextmanager
def make_client(tmp_path, monkeypatch, gateway):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "respec.db"))
    app = create_app(gateway=gateway)
    with TestClient(app) as client:
        client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
        yield client


def wait_for(cond, timeout: float = 30.0, what: str = "condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {what}")


def spec_prompts(gateway) -> list[str]:
    return [
        c["messages"][1]["content"]
        for c in gateway.calls
        if c["messages"][1]["content"].startswith("=== SPEC REQUEST ===")
    ]


def review_prompts(gateway) -> list[str]:
    return [
        c["messages"][1]["content"]
        for c in gateway.calls
        if c["messages"][1]["content"].startswith("=== AI REVIEW REQUEST ===")
    ]


def notes(story: dict) -> list[dict]:
    return [json.loads(e["data"]) for e in story["events"] if e["kind"] == "note"]


# ---- AC1: bounded loop parks at the human gate ---------------------------


def test_two_consecutive_ai_rejections_park_at_human_review(tmp_path, monkeypatch):
    def review(user: str) -> str:
        return f"nope\n\nVERDICT: REJECTED - too vague round {len(gateway.calls)}"

    gateway = ScriptedGateway(review)
    with make_client(tmp_path, monkeypatch, gateway) as client:
        story = create_story(client)
        client.post(f"/api/v1/stories/{story['id']}/queue")

        parked = wait_for_status(client, story["id"], "human_review")
        assert parked["spec_version"] == 2  # exactly two attempts, then parked
        assert parked["ai_verdict"] == "rejected"  # latest verdict kept

        detail = client.get(f"/api/v1/stories/{story['id']}").json()
        ns = notes(detail)
        assert any(
            n.get("by") == "ai-reviewer"
            and n.get("reason") == "spec rejected 2 times by AI review: too vague round 4"
            for n in ns
        ), ns

        # never re-queued past the cap: two spec rounds only
        assert len(spec_prompts(gateway)) == 2
        assert len(review_prompts(gateway)) == 2


def test_reject_limit_is_configurable(tmp_path, monkeypatch):
    def review(user: str) -> str:
        return "nope\n\nVERDICT: REJECTED - still bad"

    gateway = ScriptedGateway(review)
    with make_client(tmp_path, monkeypatch, gateway) as client:
        assert (
            client.patch("/api/v1/settings", json={"spec_reject_limit": 1}).status_code
            == 200
        )
        story = create_story(client)
        client.post(f"/api/v1/stories/{story['id']}/queue")

        parked = wait_for_status(client, story["id"], "human_review")
        assert parked["spec_version"] == 1
        assert len(spec_prompts(gateway)) == 1
        ns = notes(client.get(f"/api/v1/stories/{story['id']}").json())
        assert any(
            n.get("reason") == "spec rejected 1 times by AI review: still bad" for n in ns
        )


def test_counter_resets_on_approval_and_counts_across_human_rejects(
    tmp_path, monkeypatch
):
    verdicts = iter(["rejected", "approved", "rejected", "rejected", "approved"])

    def review(user: str) -> str:
        try:
            verdict = next(verdicts)
        except StopIteration:
            return "VERDICT: APPROVED"
        return f"VERDICT: {verdict.upper()} - note {verdict}"

    gateway = ScriptedGateway(review)
    with make_client(tmp_path, monkeypatch, gateway) as client:
        story = create_story(client)
        client.post(f"/api/v1/stories/{story['id']}/queue")

        # r1 rejected -> queued (count 1); r2 approved -> human_review (reset)
        first = wait_for_status(client, story["id"], "human_review")
        assert first["spec_version"] == 2
        assert first["spec_rejects"] == 0

        # operator rejects with a reason -> queued again
        client.post(
            f"/api/v1/stories/{story['id']}/reject", json={"reason": "operator gripe"}
        )
        # r3 rejected (count 1), r4 rejected (count 2 -> park), never r5
        parked = wait_for_status(client, story["id"], "human_review")
        assert parked["spec_version"] == 4
        assert parked["spec_rejects"] == 2
        ns = notes(client.get(f"/api/v1/stories/{story['id']}").json())
        assert any(
            n.get("reason") == "spec rejected 2 times by AI review: note rejected"
            for n in ns
        )


def test_parked_story_is_approvable_and_rejectable(tmp_path, monkeypatch):
    def review(user: str) -> str:
        return "VERDICT: REJECTED - nope"

    gateway = ScriptedGateway(review)
    with make_client(tmp_path, monkeypatch, gateway) as client:
        story = create_story(client)
        client.post(f"/api/v1/stories/{story['id']}/queue")
        wait_for_status(client, story["id"], "human_review")

        # the operator's normal gate actions still work on a parked story
        assert (
            client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "op"}).json()[
                "status"
            ]
            == "done"
        )


# ---- AC2 + AC3: labelled feedback and the rejected spec to revise --------


def test_respec_prompt_labels_and_dedupes_feedback(tmp_path, monkeypatch):
    round_no = {"n": 0}

    def review(user: str) -> str:
        round_no["n"] += 1
        if round_no["n"] == 1:
            return "VERDICT: REJECTED - ai gripe one"
        return "VERDICT: APPROVED"

    gateway = ScriptedGateway(review)
    with make_client(tmp_path, monkeypatch, gateway) as client:
        story = create_story(client)
        client.post(f"/api/v1/stories/{story['id']}/queue")
        first = wait_for_status(client, story["id"], "human_review")
        # round 1 was rejected by AI review, round 2 approved: spec is at v2
        assert first["spec_version"] == 2

        # operator rejects the approved spec twice with an overlapping reason
        client.post(f"/api/v1/stories/{story['id']}/reject", json={"reason": "needs logging"})
        wait_for_status(client, story["id"], "human_review")
        client.post(f"/api/v1/stories/{story['id']}/reject", json={"reason": "needs logging"})
        wait_for_status(client, story["id"], "human_review")
        client.post(f"/api/v1/stories/{story['id']}/reject", json={"reason": "add telemetry"})
        # round 5 regenerates and approves again — wait for its spec call,
        # not for a status (the story already sits at human_review from
        # round 4, so the status would return before round 5 starts)
        wait_for(lambda: len(spec_prompts(gateway)) >= 5, what="5 spec prompts")
        prompts = spec_prompts(gateway)
        assert len(prompts) == 5

        # round 1: first attempt — no feedback block at all
        assert "Feedback from previous rejected attempt" not in prompts[0]
        assert "Rejected spec to revise" not in prompts[0]

        # round 2 ran before any operator feedback: only the AI reason, and
        # the round-1 spec it must revise
        second = prompts[1]
        assert "ai gripe one (from AI review)" in second
        assert second.count("(from AI review)") == 1
        assert "## Rejected spec to revise" in second
        assert "(from operator)" not in second

        # round 3: operator reason + the AI reason, both labelled
        third = prompts[2]
        assert "needs logging (from operator)" in third
        assert "ai gripe one (from AI review)" in third
        # deduplicated: the repeated operator reason appears exactly once
        assert third.count("needs logging") == 1

        # round 4 still deduplicated, round 5 adds the new operator reason
        assert prompts[3].count("needs logging") == 1
        assert "needs logging (from operator)" in prompts[4]
        assert "add telemetry (from operator)" in prompts[4]


def test_respec_prompt_includes_rejected_spec_to_revise(tmp_path, monkeypatch):
    def review(user: str) -> str:
        return "VERDICT: REJECTED - always wrong"

    gateway = ScriptedGateway(review)
    with make_client(tmp_path, monkeypatch, gateway) as client:
        client.patch("/api/v1/settings", json={"spec_reject_limit": 5})
        story = create_story(client)
        client.post(f"/api/v1/stories/{story['id']}/queue")
        wait_for_status(client, story["id"], "human_review", timeout=30)

        prompts = spec_prompts(gateway)
        assert len(prompts) >= 2
        assert "## Rejected spec to revise" in prompts[1]
        # the round-1 spec text (MINIMAL_SPEC tagged "round 1") is what gets
        # revised, not a fresh document
        assert "## Goal" in prompts[1]
        assert "round 1" in prompts[1]
        assert "round 2" not in prompts[1].split("## Rejected spec to revise", 1)[1]
        assert "revise" in prompts[1].lower()
        # absent on the first attempt
        assert "Rejected spec to revise" not in prompts[0]


# ---- AC4: the reviewer must address every operator reason ---------------


def test_review_prompt_lists_operator_reasons(tmp_path, monkeypatch):
    round_no = {"n": 0}

    def review(user: str) -> str:
        round_no["n"] += 1
        if round_no["n"] == 1:
            return "VERDICT: APPROVED - fine"
        return "VERDICT: REJECTED - does not address the operator reason"

    gateway = ScriptedGateway(review)
    with make_client(tmp_path, monkeypatch, gateway) as client:
        story = create_story(client)
        client.post(f"/api/v1/stories/{story['id']}/queue")
        first = wait_for_status(client, story["id"], "human_review")
        assert first["spec_version"] == 1

        # operator rejects the approved spec with a concrete reason
        client.post(f"/api/v1/stories/{story['id']}/reject", json={"reason": "add rate limits"})
        # r2 rejects the revision (count 1), r3 rejects again (count 2) and
        # the story parks at the gate — cap 2, default
        second = wait_for_status(client, story["id"], "human_review")
        assert second["spec_version"] == 3
        assert second["ai_verdict"] == "rejected"

        reviews = review_prompts(gateway)
        assert len(reviews) == 3
        # first review ran before any operator feedback: no section at all
        assert "Operator reasons" not in reviews[0]
        assert "## Operator reasons the revised spec must address" in reviews[1]
        assert "add rate limits" in reviews[1]
        assert "must address" in reviews[1]
        # any unaddressed reason must produce a rejection
        assert "VERDICT: REJECTED" in reviews[1]
        # the section persists on later rounds too
        assert "add rate limits" in reviews[2]


# ---- AC5: spec history ---------------------------------------------------


def test_spec_history_exposed_for_every_version(tmp_path, monkeypatch):
    round_no = {"n": 0}

    def review(user: str) -> str:
        round_no["n"] += 1
        if round_no["n"] == 1:
            return "VERDICT: REJECTED - v1 was vague"
        return "VERDICT: APPROVED - good now"

    gateway = ScriptedGateway(review)
    with make_client(tmp_path, monkeypatch, gateway) as client:
        story = create_story(client)
        client.post(f"/api/v1/stories/{story['id']}/queue")
        final = wait_for_status(client, story["id"], "human_review")

        assert final["spec_version"] == 2
        history = final["spec_history"]
        assert isinstance(history, list) and len(history) == 2
        assert history[0]["version"] == 1
        assert history[0]["verdict"] == "rejected"
        assert history[0]["reason"] == "v1 was vague"
        assert history[0]["timestamp"]
        assert history[1]["version"] == 2
        assert history[1]["verdict"] == "approved"
        assert history[1]["reason"] is None


def test_spec_history_cleared_on_respec(tmp_path, monkeypatch):
    def review(user: str) -> str:
        return "VERDICT: REJECTED - x"

    gateway = ScriptedGateway(review)
    with make_client(tmp_path, monkeypatch, gateway) as client:
        story = create_story(client)
        client.post(f"/api/v1/stories/{story['id']}/queue")
        parked = wait_for_status(client, story["id"], "human_review")
        assert parked["spec_history"]

        from sqlmodel import Session

        from app.models import Story

        engine = client.app.state.engine
        with Session(engine) as db:
            s = db.get(Story, story["id"])
            # "backlog", not "queued": a queued story is a scheduler
            # candidate, so the runner could claim it before /re-spec lands
            # and its 409 guard would turn this into a race.
            s.status = "backlog"  # /re-spec only accepts retryable statuses
            s.ai_verdict = None
            db.add(s)
            db.commit()

        out = client.post(f"/api/v1/stories/{story['id']}/re-spec", json={}).json()
        assert out["spec_version"] == 0
        assert out["spec_history"] == []
        assert out["spec_rejects"] == 0


# ---- guardrail: happy-path prompts byte-identical ------------------------


def test_happy_path_prompt_is_byte_identical_without_rejections(tmp_path):
    from app.models import Story, StoryKind
    from app.runner import _build_review_prompt, _build_user_prompt

    story = Story(title="Add login", description="Users log in", acceptance="login works")
    story.model = "echo/dev"
    story.kind = StoryKind.CODE
    base_user = _build_user_prompt(story, [])
    assert "echo/dev" in base_user
    # the new optional args must not change the rendered prompt
    assert _build_user_prompt(story, [], last_spec=None) == base_user
    assert _build_user_prompt(story, [], last_spec="") == base_user

    base_review = _build_review_prompt(story)
    assert _build_review_prompt(story, []) == base_review
    assert "Operator reasons" not in base_review


# ---- feedback hygiene: bookkeeping notes and the approval cut-off --------


def _runner() -> "object":
    """A StoryRunner shell: _reject_feedback only touches the passed session,
    so gateway/registry are irrelevant here."""
    from app.runner import StoryRunner

    return object.__new__(StoryRunner)


def _feedback_session(tmp_path):
    """In-memory DB with one story, yielded with a session factory."""
    from sqlmodel import Session, SQLModel, create_engine

    engine = create_engine(
        f"sqlite:///{tmp_path / 'feedback.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    from app.models import Story

    with Session(engine) as db:
        story = Story(title="Add login", status="queued")
        db.add(story)
        db.commit()
        # captured inside the session: the instance is detached (and its
        # attributes expired) once it closes
        story_id = story.id
    return engine, story_id


def test_operator_bookkeeping_notes_are_not_feedback(tmp_path, monkeypatch):
    """Re-spec / retry / lease-release notes are an audit trail, not a gripe:
    they must never reach the spec prompt."""
    from sqlmodel import Session, select

    from app.models import StoryEvent
    from app.runner import record_event

    engine, story_id = _feedback_session(tmp_path)
    with Session(engine) as db:
        record_event(
            db, story_id, "note", {"reason": "re-spec by operator", "by": "op", "feedback": False}
        )
        record_event(
            db,
            story_id,
            "note",
            {"reason": "operator retry from stage queued", "by": "op", "feedback": False},
        )
        record_event(
            db,
            story_id,
            "note",
            {"reason": "lease expired: no activity for 2h, auto-released", "by": "scheduler",
             "feedback": False},
        )
        db.commit()

        runner = _runner()
        assert runner._reject_feedback(db, story_id) == []
        # and the note events are still there (skipped, not deleted)
        assert db.exec(select(StoryEvent).where(StoryEvent.kind == "note")).all()


def test_human_spec_approval_cuts_off_operator_reasons(tmp_path):
    """A human approving the spec closes that revision's feedback loop:
    reasons recorded before the approval are stale by the time the story is
    specced again (failed coder run -> re-spec)."""
    from sqlmodel import Session

    from app.runner import RejectFeedback, record_event

    engine, story_id = _feedback_session(tmp_path)
    with Session(engine) as db:
        record_event(db, story_id, "note", {"reason": "stale gripe", "by": "op"})
        record_event(db, story_id, "ai_review", {"verdict": "approved"})
        record_event(db, story_id, "spec_approved", {"by": "op"})
        record_event(db, story_id, "note", {"reason": "re-spec by operator", "by": "op",
                                            "feedback": False})
        db.commit()

        runner = _runner()
        assert runner._reject_feedback(db, story_id) == []

        # a fresh objection after the approval still counts
        record_event(db, story_id, "note", {"reason": "fresh gripe", "by": "op"})
        db.commit()
        assert runner._reject_feedback(db, story_id) == [
            RejectFeedback(reason="fresh gripe", source="operator")
        ]


def test_respec_note_never_reaches_the_spec_prompt(tmp_path, monkeypatch):
    """End to end: the note POST /re-spec records is bookkeeping — the next
    spec prompt must not list it as operator feedback."""

    def review(user: str) -> str:
        return "VERDICT: APPROVED - fine"

    gateway = ScriptedGateway(review)
    with make_client(tmp_path, monkeypatch, gateway) as client:
        story = create_story(client)
        client.post(f"/api/v1/stories/{story['id']}/queue")
        wait_for_status(client, story["id"], "human_review")

        from sqlmodel import Session

        from app.models import Story

        engine = client.app.state.engine
        with Session(engine) as db:
            s = db.get(Story, story["id"])
            # "backlog", not "queued": a queued story is a scheduler
            # candidate, so the runner could claim it before /re-spec lands
            # and its 409 guard would turn this into a race.
            s.status = "backlog"  # /re-spec only accepts retryable statuses
            s.ai_verdict = None
            db.add(s)
            db.commit()

        out = client.post(f"/api/v1/stories/{story['id']}/re-spec", json={}).json()
        assert out["status"] == "queued"
        wait_for(lambda: len(spec_prompts(gateway)) >= 2, what="2 spec prompts")
        last = spec_prompts(gateway)[-1]
        assert "re-spec by operator" not in last
        assert "Feedback from previous rejected attempt" not in last


def test_agent_authored_notes_are_not_operator_reasons(tmp_path):
    """Gate round 2, point 1: only HUMAN reject reasons are "from operator".
    Notes written by the pipeline itself (code reviewer, vendoring guard,
    the lease scheduler) must never reach the spec writer or the spec
    reviewer's "the spec must address" list as if the operator said them."""
    from sqlmodel import Session

    from app.runner import LEASE_RELEASED_BY, RejectFeedback, record_event

    engine, story_id = _feedback_session(tmp_path)
    with Session(engine) as db:
        record_event(db, story_id, "note", {
            "reason": "verify failed after 3 attempts: warning: VIRTUAL_ENV ...",
            "by": "code-reviewer"})
        record_event(db, story_id, "note", {
            "reason": "code review produced no verdict", "by": "code-reviewer"})
        record_event(db, story_id, "note", {
            "reason": "vendored packages added: .local-test-deps/", "by": "vendored-guard"})
        record_event(db, story_id, "note", {
            "reason": "lease expired", "by": LEASE_RELEASED_BY})
        record_event(db, story_id, "note", {"reason": "human gripe", "by": "coordinator"})
        db.commit()

        assert _runner()._reject_feedback(db, story_id) == [
            RejectFeedback(reason="human gripe", source="operator")
        ]


def test_code_review_finish_never_writes_spec_history(tmp_path):
    """Gate round 2, point 2: spec_history holds SPEC-review verdicts only.
    _finish is shared with the code review (_run_code_review passes
    ai_review/ai_verdict too); a code-review verdict must not become a
    spec-history row. The spec review still writes its rows (see
    test_spec_history_exposed_for_every_version)."""
    from sqlmodel import Session

    from app.models import Story, StoryStatus

    engine, story_id = _feedback_session(tmp_path)
    with Session(engine) as db:
        story = db.get(Story, story_id)
        story.status = StoryStatus.AI_REVIEW
        story.spec = "spec v1"
        story.spec_version = 1
        db.add(story)
        db.commit()

    runner = _runner()
    runner.engine = engine
    # exactly how _run_code_review finishes an approved code review
    runner._finish(
        story_id,
        StoryStatus.HUMAN_REVIEW,
        StoryStatus.AI_REVIEW,
        ai_review="looks good\n\nVERDICT: APPROVED",
        ai_verdict="approved",
    )
    with Session(engine) as db:
        assert (db.get(Story, story_id).spec_history or "[]") == "[]"
