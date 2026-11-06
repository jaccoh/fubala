"""Story CRUD, guardrails, and the runner pipeline (spec + AI review)."""

import json
import time

import pytest
from conftest import MINIMAL_SPEC


def create_story(client, **body) -> dict:
    res = client.post("/api/v1/stories", json={"title": "Add login", **body})
    assert res.status_code == 201
    return res.json()


def wait_for_status(client, story_id: str, want: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    story = None
    while time.time() < deadline:
        story = client.get(f"/api/v1/stories/{story_id}").json()
        if story["status"] == want:
            return story
        time.sleep(0.05)
    pytest.fail(f"story never reached {want}; last status: {story and story['status']}")


class SequencedGateway:
    """Returns canned responses in order, one per stream_chat call."""

    def __init__(self, responses: list[list[str]]):
        self.responses = [list(r) for r in responses]
        self.calls: list[dict] = []

    async def stream_chat(
        self, *, model: str, messages: list[dict], tools=None, max_output_tokens=None
    ):
        from app.providers import Delta, Usage

        self.calls.append({"model": model, "messages": messages, "tools": tools})
        for chunk in self.responses.pop(0):
            yield Delta(text=chunk)
        yield Usage(prompt_tokens=1, completion_tokens=1)


# ---- CRUD & guardrails -------------------------------------------------


def test_story_crud_lifecycle(client):
    story = create_story(client, description="Users need to log in", acceptance="login works")
    assert story["status"] == "backlog"
    assert story["model"] == "echo/dev"
    assert story["created_by"] == "local"
    assert story["spec_version"] == 0
    assert story["ai_verdict"] is None

    assert client.get("/api/v1/stories").json()[0]["id"] == story["id"]
    detail = client.get(f"/api/v1/stories/{story['id']}").json()
    assert detail["events"][0]["kind"] == "created"

    patched = client.patch(
        f"/api/v1/stories/{story['id']}", json={"title": "Add SSO login"}
    ).json()
    assert patched["title"] == "Add SSO login"

    assert client.delete(f"/api/v1/stories/{story['id']}").status_code == 204
    assert client.get(f"/api/v1/stories/{story['id']}").status_code == 404


def test_create_story_unknown_model_rejected(client):
    assert client.post("/api/v1/stories", json={"title": "x", "model": "nope/x"}).status_code == 400


def test_transition_guardrails(tmp_path, monkeypatch, gateway):
    # scheduler HARD-off: stop() awaits the cancelled loop task, so no tick —
    # not even one already sleeping on the default 0.5s interval — can fire
    # between the queue/unqueue transitions below and claim the story
    # (queued -> in_progress made those transitions 409; that race flaked
    # under xdist worker load and no sleep-based park closes the window).
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "guardrails.db"))
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(gateway=gateway)) as client:
        client.portal.call(client.app.state.story_runner.stop)
        story = create_story(client)

        # queue from backlog is fine; double-queue and unqueue-from-backlog are not
        assert client.post(f"/api/v1/stories/{story['id']}/queue").status_code == 200
        assert client.post(f"/api/v1/stories/{story['id']}/queue").status_code == 409
        assert client.post(f"/api/v1/stories/{story['id']}/unqueue").json()["status"] == "backlog"
        assert client.post(f"/api/v1/stories/{story['id']}/unqueue").status_code == 409

        # the human gate: approve/reject only from human_review
        assert client.post(f"/api/v1/stories/{story['id']}/approve", json={}).status_code == 409
        assert client.post(
            f"/api/v1/stories/{story['id']}/reject", json={"reason": "not in review"}
        ).status_code == 409

        # ai_review is runner-owned: a human cannot approve from there either
        _set_status(client, story["id"], "ai_review")
        assert client.post(f"/api/v1/stories/{story['id']}/approve", json={}).status_code == 409


def test_story_immutable_while_in_flight(client):
    story = create_story(client)
    _set_status(client, story["id"], "human_review")
    res = client.patch(f"/api/v1/stories/{story['id']}", json={"title": "nope"})
    assert res.status_code == 409


def _set_status(client, story_id: str, status: str):
    """Bypass actions to place a story in a state only the runner reaches."""
    from sqlmodel import Session

    from app.models import Story

    engine = client.app.state.engine
    with Session(engine) as db:
        s = db.get(Story, story_id)
        s.status = status
        s.spec = "## Goal\nfake"
        db.add(s)
        db.commit()
    return None


def _add_event(client, story_id: str, kind: str, data: dict):
    from sqlmodel import Session

    from app.models import StoryEvent

    with Session(client.app.state.engine) as db:
        db.add(StoryEvent(story_id=story_id, kind=kind, data=json.dumps(data)))
        db.commit()


def _started(call_id: str, at: str) -> dict:
    return {
        "call_id": call_id,
        "name": "run_verify",
        "args": "{}",
        "started_at": at,
        "timeout_s": 600,
    }


def _running(client, story_id: str) -> tuple[list, list]:
    listed = next(s for s in client.get("/api/v1/stories").json() if s["id"] == story_id)
    one = client.get(f"/api/v1/stories/{story_id}").json()
    return listed["running_tools"], one["running_tools"]


def test_running_tools_open_calls_oldest_first(client):
    # scheduler hard-off: an `executing` row with a spec is a restart orphan the
    # live scheduler would resume, racing the events this test seeds
    client.portal.call(client.app.state.story_runner.stop)
    story = create_story(client)
    sid = story["id"]
    _set_status(client, sid, "executing")
    _add_event(client, sid, "tool_started", _started("old", "2025-01-01T00:00:00.000Z"))
    _add_event(client, sid, "round", {"round": 2})
    _add_event(client, sid, "tool_started", _started("b", "2025-01-01T00:00:10.000Z"))
    _add_event(client, sid, "tool_started", _started("a", "2025-01-01T00:00:05.000Z"))
    for tools in _running(client, sid):
        assert [t["call_id"] for t in tools] == ["a", "b"]
    _add_event(client, sid, "tool_finished", {"call_id": "a"})
    for tools in _running(client, sid):
        assert [t["call_id"] for t in tools] == ["b"]
    _set_status(client, sid, "human_review")
    assert _running(client, sid) == ([], [])


# ---- runner pipeline ----------------------------------------------------


def test_queue_runs_spec_then_ai_review_to_human_review(client, gateway):
    story = create_story(client)
    client.post(f"/api/v1/stories/{story['id']}/queue")

    done = wait_for_status(client, story["id"], "human_review")
    assert done["spec_version"] == 1
    assert done["spec"] == MINIMAL_SPEC  # FakeGateway emits a valid spec
    assert done["ai_review"] == "Hello there!"  # review stage ran too (default chunks)
    assert done["ai_verdict"] == "approved"  # no VERDICT line -> fail-open approve
    assert done["error"] is None

    kinds = [e["kind"] for e in client.get(f"/api/v1/stories/{story['id']}").json()["events"]]
    assert kinds[0] == "created"
    assert "spec" in kinds and "ai_review" in kinds

    # stage 1 asked for a spec; stage 2 asked for a review of that spec
    assert gateway.calls[0]["messages"][1]["content"].startswith("=== SPEC REQUEST ===")
    assert "spec writer" in gateway.calls[0]["messages"][0]["content"].lower()
    review_call = gateway.calls[1]["messages"][1]["content"]
    assert review_call.startswith("=== AI REVIEW REQUEST ===")
    assert "Add the thing." in review_call  # the spec under review


# ---- harness-injected context files -------------------------------------


def test_context_files_block_matching_and_priority(tmp_path):
    from app.runner import context_files_block

    root = tmp_path / "repo"
    root.mkdir()
    (root / "AGENTS.md").write_text("Prefer uv.\n")
    (root / "HANDOVER.md").write_text("State lives in store.ts.\n")

    settings = {"context_files": "AGENTS.md, HANDOVER.md, MISSING.md"}
    block = context_files_block(settings, [str(root)])
    assert "PROJECT INSTRUCTIONS: AGENTS.md" in block and "Prefer uv." in block
    assert "HANDOVER.md" in block and "store.ts" in block
    assert "MISSING" not in block  # absent globs are skipped silently

    # earlier roots win for the same file
    other = tmp_path / "other"
    other.mkdir()
    (other / "AGENTS.md").write_text("shadowed content\n")
    block = context_files_block(settings, [str(other), str(root)])
    assert "shadowed content" in block and "Prefer uv." not in block

    # glob patterns and empty settings behave
    assert "AGENTS.md" in context_files_block({"context_files": "*.md"}, [str(root)])
    assert context_files_block({"context_files": ""}, [str(root)]) == ""


def test_context_files_block_caps_huge_files(tmp_path):
    from app.runner import CONTEXT_FILE_CAP, context_files_block

    root = tmp_path
    (root / "AGENTS.md").write_text("x" * (CONTEXT_FILE_CAP + 5000))
    block = context_files_block({"context_files": "AGENTS.md"}, [str(root)])
    assert len(block) < CONTEXT_FILE_CAP + 500


def test_spec_writer_gets_context_files_injected(client, tmp_path, gateway):
    project = tmp_path / "ctxrepo"
    project.mkdir()
    (project / "AGENTS.md").write_text("Convention: always add tests.\n")
    client.patch("/api/v1/settings", json={"project_root": str(project)})

    story = create_story(client)
    client.post(f"/api/v1/stories/{story['id']}/queue")
    wait_for_status(client, story["id"], "human_review")

    system = gateway.calls[0]["messages"][0]["content"]
    assert "PROJECT INSTRUCTIONS: AGENTS.md" in system
    assert "always add tests" in system


def test_approve_completes_and_records_approver(client):
    story = create_story(client)
    client.post(f"/api/v1/stories/{story['id']}/queue")
    wait_for_status(client, story["id"], "human_review")

    res = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "owner"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "done"
    assert body["approved_by"] == "owner"

    # done is terminal
    assert client.post(f"/api/v1/stories/{story['id']}/queue").status_code == 409


def test_reject_feeds_back_into_next_spec_run(client, gateway):
    story = create_story(client)
    client.post(f"/api/v1/stories/{story['id']}/queue")
    wait_for_status(client, story["id"], "human_review")

    res = client.post(
        f"/api/v1/stories/{story['id']}/reject",
        json={"reason": "missing error handling", "by": "owner"},
    )
    assert res.json()["status"] == "queued"

    wait_for_status(client, story["id"], "human_review")
    story = client.get(f"/api/v1/stories/{story['id']}").json()
    assert story["spec_version"] == 2

    # rejection reason was included in the second spec request
    assert "missing error handling" in gateway.calls[-2]["messages"][1]["content"]
    notes = [e for e in story["events"] if e["kind"] == "note"]
    assert any("missing error handling" in json.loads(e["data"]).get("reason", "") for e in notes)


def test_ai_reviewer_rejection_requeues_with_feedback(tmp_path, monkeypatch):
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "aireject.db"))
    from fastapi.testclient import TestClient

    from app.main import create_app

    gateway = SequencedGateway(
        [
            [MINIMAL_SPEC],  # spec v1
            ["vague steps\n\nVERDICT: REJECTED - steps are not verifiable"],  # ai review rejects
            # spec v2 (with feedback) — ends on a terminal line so the
            # deterministic preflight lets it through to the reviewer
            [MINIMAL_SPEC + "second draft.\n"],
            ["fixed\n\nVERDICT: APPROVED"],  # ai review approves
        ]
    )
    with TestClient(create_app(gateway=gateway)) as client:
        client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
        story = create_story(client)
        client.post(f"/api/v1/stories/{story['id']}/queue")

        # bounced back to queued by the AI reviewer, then re-run to approval
        final = wait_for_status(client, story["id"], "human_review")
        assert final["spec_version"] == 2
        assert final["ai_verdict"] == "approved"

        detail = client.get(f"/api/v1/stories/{story['id']}").json()
        notes = [json.loads(e["data"]) for e in detail["events"] if e["kind"] == "note"]
        assert any(
            n.get("by") == "ai-reviewer" and "not verifiable" in n.get("reason", "") for n in notes
        )

        # the AI rejection reason reached the second spec request
        assert "not verifiable" in gateway.calls[2]["messages"][1]["content"]
        # and the second review saw spec v2
        assert "second draft" in gateway.calls[3]["messages"][1]["content"]


def test_failed_story_can_be_requeued(client, monkeypatch, tmp_path):
    from app.providers import Delta, GatewayError

    class FlakyGateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            self.calls += 1
            if self.calls == 1:
                raise GatewayError("[echo] key exploded")
            yield Delta(text=MINIMAL_SPEC)

    from fastapi.testclient import TestClient

    from app.main import create_app

    # per-test DB (tmp_path is worker-unique under xdist; fixed /tmp paths
    # collide between workers and between parallel stories)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "flaky.db"))
    app = create_app(gateway=FlakyGateway())
    with TestClient(app) as c:
        c.patch("/api/v1/settings", json={"story_poll_interval": 0.1})
        story = create_story(c)
        c.post(f"/api/v1/stories/{story['id']}/queue")
        failed = wait_for_status(c, story["id"], "failed")
        assert "[echo] key exploded" in failed["error"]

        # failed -> queued is a legal retry
        assert c.post(f"/api/v1/stories/{story['id']}/queue").json()["status"] == "queued"
        wait_for_status(c, story["id"], "human_review")


def test_rate_limit_requeues_with_backoff(client, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers import GatewayError

    class LimitedGateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta

            self.calls += 1
            if self.calls <= 2:  # spec attempt + retry both limited
                raise GatewayError(
                    "[zai] RateLimitError: AnthropicException - rate_limit_error 1302"
                )
            yield Delta(text=MINIMAL_SPEC)

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "rl.db"))
    gateway = LimitedGateway()
    app = create_app(gateway=gateway)
    # shrink the backoff so the test doesn't wait minutes
    from app import runner as runner_mod

    original = runner_mod.StoryRunner._requeue_for_rate_limit

    def fast_requeue(self, story_id, error, reason="rate limit", **kw):
        original(self, story_id, error, reason=reason, **kw)
        if story_id in self._backoff:  # shrink the real backoff to ~1s
            not_before, attempt = self._backoff[story_id]
            self._backoff[story_id] = (__import__("time").monotonic() + 1, attempt)

    runner_mod.StoryRunner._requeue_for_rate_limit = fast_requeue
    try:
        with TestClient(app) as c:
            c.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
            story = create_story(c)
            c.post(f"/api/v1/stories/{story['id']}/queue")

            # first attempt is rate-limited -> requeued (NOT failed)
            deadline = time.time() + 30
            requeued = False
            while time.time() < deadline and not requeued:
                s = c.get(f"/api/v1/stories/{story['id']}").json()
                assert s["status"] != "failed", s.get("error")
                if gateway.calls >= 1 and s["status"] == "queued":
                    requeued = True
                time.sleep(0.05)
            assert requeued, "never requeued after the limited attempt"

            # retry (after 1s fast backoff) is also limited -> requeued again
            deadline = time.time() + 30
            while time.time() < deadline and gateway.calls < 2:
                time.sleep(0.1)
            assert gateway.calls >= 2, "retry never happened"
            notes = [
                e["data"] for e in c.get(f"/api/v1/stories/{story['id']}").json()["events"]
                if "rate limit" in e["data"]
            ]
            assert notes, "expected rate-limit notes"
    finally:
        runner_mod.StoryRunner._requeue_for_rate_limit = original


# ---- transient gateway errors requeue like rate limits --------------------


def _fast_backoff(monkeypatch):
    """Shrink _requeue_for_rate_limit's real backoff to ~1s, same trick as the
    rate-limit test above."""
    from app import runner as runner_mod

    original = runner_mod.StoryRunner._requeue_for_rate_limit

    def fast_requeue(self, story_id, error, reason="rate limit", **kw):
        original(self, story_id, error, reason=reason, **kw)
        if story_id in self._backoff:  # shrink the real backoff to ~1s
            not_before, attempt = self._backoff[story_id]
            self._backoff[story_id] = (__import__("time").monotonic() + 1, attempt)

    monkeypatch.setattr(runner_mod.StoryRunner, "_requeue_for_rate_limit", fast_requeue)


def test_is_transient_error_matches_markers_and_rejects_hard_errors():
    from app.runner import _is_transient_error

    for marker in (
        "[zai] MidStreamFallbackError: stream broke",  # midstream
        "APIConnectionError: gateway unreachable",  # apiconnection
        "connection timed out after 30s",
        "connection closed by peer",
        "Timeout on reading data from socket",
        # provider 5xx / z.ai "internal network failure" shapes
        "[zai] InternalServerError: litellm.InternalServerError",
        "[zai] InternalServerError: Internal Server Error (500)",
        "litellm.ServiceUnavailableError: the provider is overloaded",
        "[zai] ServiceUnavailableError: 503 Service Unavailable",
        'z.ai error {"error":{"code":"1234","message":"Internal network failure. '
        'Please try again later."}}',
        "internal network failure — please try again later",
    ):
        assert _is_transient_error(marker), marker
    # hard failures must NOT requeue: 4xx/auth shapes and unrelated text
    for hard in (
        "[echo] key exploded",
        "litellm.BadRequestError: 400 invalid request payload",
        "[zai] AuthenticationError: invalid api key",
        "litellm.PermissionDeniedError: 403 forbidden",
        "",
        "processed 1500 stories",  # no bare-"500" false positive
    ):
        assert not _is_transient_error(hard), hard


def test_transient_error_requeues_spec_stage(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers import Delta, GatewayError

    class MidstreamGateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            self.calls += 1
            if self.calls == 1:
                raise GatewayError("[zai] MidStreamFallbackError: connection closed")
            yield Delta(text=MINIMAL_SPEC)

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "transient-spec.db"))
    gateway = MidstreamGateway()
    app = create_app(gateway=gateway)
    _fast_backoff(monkeypatch)
    with TestClient(app) as c:
        c.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
        story = create_story(c)
        c.post(f"/api/v1/stories/{story['id']}/queue")

        # first spec attempt dies midstream -> back to queued, NOT failed
        deadline = time.time() + 30
        requeued = False
        while time.time() < deadline and not requeued:
            s = c.get(f"/api/v1/stories/{story['id']}").json()
            assert s["status"] != "failed", s.get("error")
            if gateway.calls >= 1 and s["status"] == "queued":
                requeued = True
            time.sleep(0.05)
        assert requeued, "midstream error never requeued the story"

        # then the retry (after ~1s fast backoff) completes
        final = wait_for_status(c, story["id"], "human_review", timeout=30)

        # the requeue is recorded as a status event carrying a note
        requeue_events = [
            json.loads(e["data"])
            for e in final["events"]
            if "provider error; retry in ~" in e["data"] and "(attempt 1)" in e["data"]
        ]
        assert requeue_events, "midstream: expected a requeue note event"
        assert "(attempt 1)" in requeue_events[0]["note"]


# ---- provider 5xx / z.ai 1234 requeue like rate limits ----------------------


def _provider_error_message(shape: str) -> str:
    """Realistic GatewayError strings for litellm exception flattening."""
    if shape == "InternalServerError":
        return (
            '[zai] InternalServerError: litellm.InternalServerError: z.ai error '
            '{"error":{"code":"1234","message":"Internal network failure. '
            'Please try again later."}}'
        )
    if shape == "ServiceUnavailableError":
        return "[zai] ServiceUnavailableError: 503 Service Unavailable"
    return "APIConnectionError: connection timed out after 30s"


@pytest.mark.parametrize(
    "shape", ["InternalServerError", "ServiceUnavailableError", "APIConnectionError"]
)
def test_provider_5xx_requeues_spec_stage(monkeypatch, tmp_path, shape):
    """litellm 5xx/connection errors requeue instead of failing; the retry
    lands the story in human_review and the note uses the 'provider error'
    wording."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers import Delta, GatewayError

    class Flaky5xxGateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            self.calls += 1
            if self.calls == 1:
                raise GatewayError(_provider_error_message(shape))
            yield Delta(text=MINIMAL_SPEC)

    # per-test DB (tmp_path is worker-unique under xdist)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / f"5xx-{shape}.db"))
    gateway = Flaky5xxGateway()
    app = create_app(gateway=gateway)
    _fast_backoff(monkeypatch)
    with TestClient(app) as c:
        c.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
        story = create_story(c)
        c.post(f"/api/v1/stories/{story['id']}/queue")

        # attempt 1 dies with a transient 5xx -> back to queued, NOT failed
        deadline = time.time() + 30
        requeued = False
        while time.time() < deadline and not requeued:
            s = c.get(f"/api/v1/stories/{story['id']}").json()
            assert s["status"] != "failed", s.get("error")
            if gateway.calls >= 1 and s["status"] == "queued":
                requeued = True
            time.sleep(0.05)
        assert requeued, f"{shape} error never requeued the story"

        # then the retry (after ~1s fast backoff) completes
        final = wait_for_status(c, story["id"], "human_review", timeout=30)
        requeue_events = [
            json.loads(e["data"])
            for e in final["events"]
            if "provider error; retry in ~" in e["data"]
        ]
        assert requeue_events, f"{shape}: expected a 'provider error' requeue note"
        import re as _re

        assert _re.search(
            r"provider error; retry in ~\d+ min \(attempt \d+\)",
            requeue_events[0]["note"],
        )


def test_provider_5xx_fails_after_cap(monkeypatch, tmp_path):
    """Always-failing gateway + retry cap: the story ends failed with the last
    error after max_provider_retries attempts."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers import GatewayError

    err = _provider_error_message("InternalServerError")

    class Always5xxGateway:
        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            raise GatewayError(err)
            yield  # pragma: no cover

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "5xx-cap.db"))
    app = create_app(gateway=Always5xxGateway())
    _fast_backoff(monkeypatch)
    with TestClient(app) as c:
        c.patch(
            "/api/v1/settings",
            json={"story_poll_interval": 0.05, "max_provider_retries": 2},
        )
        story = create_story(c)
        c.post(f"/api/v1/stories/{story['id']}/queue")

        final = wait_for_status(c, story["id"], "failed", timeout=30)
        assert err in final["error"]
        # spec: fail at attempt > N, so N=2 retries (attempts 1 and 2) requeue
        # before attempt 3 gives up -> exactly two "retry in ~" notes
        attempts = [
            json.loads(e["data"]).get("attempt")
            for e in final["events"]
            if e["kind"] == "status" and "retry in ~" in e["data"]
        ]
        assert attempts == [1, 2], f"expected two requeues before the cap: {attempts}"


def test_rate_limit_is_not_capped_by_max_provider_retries(monkeypatch, tmp_path):
    """A provider QUOTA window (z.ai 1302) can last hours: rate limits keep
    the patient unbounded requeue. max_provider_retries caps transport/5xx
    errors only -- a capped rate limit would fail a whole chain overnight."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers import GatewayError

    err = "[zai] RateLimitError: litellm.RateLimitError: 1302 rate limit reached"

    class AlwaysRateLimitedGateway:
        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            raise GatewayError(err)
            yield  # pragma: no cover

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "ratelimit-cap.db"))
    app = create_app(gateway=AlwaysRateLimitedGateway())
    _fast_backoff(monkeypatch)
    with TestClient(app) as c:
        c.patch(
            "/api/v1/settings",
            json={"story_poll_interval": 0.05, "max_provider_retries": 1},
        )
        story = create_story(c)
        c.post(f"/api/v1/stories/{story['id']}/queue")

        deadline = time.time() + 30
        requeues = 0
        while time.time() < deadline and requeues < 3:
            s = c.get(f"/api/v1/stories/{story['id']}").json()
            assert s["status"] != "failed", s.get("error")
            requeues = sum(
                1 for e in s["events"] if e["kind"] == "status" and "retry in ~" in e["data"]
            )
            time.sleep(0.1)
        assert requeues >= 3, f"rate limit must keep requeueing past the cap: {requeues}"


@pytest.mark.parametrize(
    "hard",
    [
        "litellm.BadRequestError: 400 invalid request payload",
        "[zai] AuthenticationError: invalid api key",
    ],
)
def test_hard_provider_errors_fail_without_retry(monkeypatch, tmp_path, hard):
    """4xx/auth-shaped gateway errors are not transient: the story fails on
    attempt 1 with no requeue notes at all."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers import GatewayError

    class HardGateway:
        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            raise GatewayError(hard)
            yield  # pragma: no cover

    # per-test DB (tmp_path is worker-unique under xdist)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "hard.db"))
    with TestClient(create_app(gateway=HardGateway())) as c:
        c.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
        story = create_story(c)
        c.post(f"/api/v1/stories/{story['id']}/queue")

        final = wait_for_status(c, story["id"], "failed", timeout=30)
        assert hard in final["error"]
        requeues = [e for e in final["events"] if "retry in ~" in e["data"]]
        assert not requeues, f"hard error requeued the story: {requeues}"


def test_dep_waits_through_transient_backoff_and_cascade(monkeypatch, tmp_path):
    """A depends on B. B transient-requeues -> A stays queued (never failed).
    B permanently fails -> A failed 'dependency failed: ...'. B cancelled ->
    A failed too."""
    import asyncio

    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers import Delta, GatewayError

    transient_err = _provider_error_message("InternalServerError")

    class DepGateway:
        """Scripted by mode: one transient 5xx then a real backoff window
        ('transient'), an unconditional hard error ('permanent'), or a stream
        that hangs until the run is cancelled ('hang')."""

        def __init__(self):
            self.mode = "transient"
            self.calls = 0
            self.hang = asyncio.Event()

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            self.calls += 1
            if self.mode == "hang":
                yield Delta(text="## Goal\nhanging\n")
                await self.hang.wait()  # cancelled via the cancel endpoint
                return
            if self.mode == "permanent":
                raise GatewayError("[echo] kaput for good")
            raise GatewayError(transient_err)  # mode == "transient"
            yield  # pragma: no cover

    # per-test DB (tmp_path is worker-unique under xdist)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "dep-5xx.db"))
    gateway = DepGateway()
    app = create_app(gateway=gateway)
    with TestClient(app) as c:
        c.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
        runner = app.state.story_runner

        b = create_story(c, title="prereq B")
        a = create_story(c, title="dependent A", depends_on=[b["id"]])
        c.post(f"/api/v1/stories/{b['id']}/queue")
        c.post(f"/api/v1/stories/{a['id']}/queue")

        # phase 1: B's spec attempt dies with a transient 5xx -> requeued with
        # the REAL (5 min) backoff; A must sit in queued the whole time
        deadline = time.time() + 30
        while time.time() < deadline:
            sa = c.get(f"/api/v1/stories/{a['id']}").json()
            assert sa["status"] == "queued", sa
            assert not sa["error"], "A must not fail while B retries"
            sb = c.get(f"/api/v1/stories/{b['id']}").json()
            if (
                gateway.calls >= 1
                and sb["status"] == "queued"
                and b["id"] in runner._backoff
            ):
                break
            time.sleep(0.05)
        else:
            pytest.fail("B never requeued with a backoff slot")

        # phase 2: flip to a permanent hard error and open the backoff window
        # now (no sleeping through the real delay); B's retry fails for good
        # -> A cascades to failed with the dependency error
        gateway.mode = "permanent"
        not_before, attempt = runner._backoff[b["id"]]
        runner._backoff[b["id"]] = (time.monotonic() - 1, attempt)
        a_final = wait_for_status(c, a["id"], "failed", timeout=30)
        assert "dependency failed: prereq B" in a_final["error"]
        b_error = c.get(f"/api/v1/stories/{b['id']}").json()["error"]
        assert "[echo] kaput for good" in b_error

        # phase 3: fresh pair — B2 runs with a hung stream, gets cancelled,
        # and A2 fails like it did before transient requeueing existed
        gateway.mode = "hang"
        b2 = create_story(c, title="prereq B2")
        a2 = create_story(c, title="dependent A2", depends_on=[b2["id"]])
        c.post(f"/api/v1/stories/{b2['id']}/queue")
        c.post(f"/api/v1/stories/{a2['id']}/queue")
        wait_for_status(c, b2["id"], "in_progress", timeout=30)
        assert c.post(f"/api/v1/stories/{b2['id']}/cancel").status_code == 200
        a2_final = wait_for_status(c, a2["id"], "failed", timeout=30)
        assert "dependency failed" in a2_final["error"]


# ---- retry/requeue re-opens cascade-failed dependents ----------------------


class _HangGateway:
    """Streams a spec fragment, then blocks forever: the run is ended by
    POST /cancel, which is the cancel path through runner._fail (and so the
    dependency cascade)."""

    def __init__(self):
        import asyncio

        self.hang = asyncio.Event()

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
        from app.providers import Delta

        yield Delta(text="## Goal\nhanging\n")
        await self.hang.wait()  # cancelled via the cancel endpoint
        return  # pragma: no cover


def _reopen_client(monkeypatch, tmp_path, name: str, gateway):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / name))
    client = TestClient(create_app(gateway=gateway))
    client.__enter__()
    client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
    return client


def _fail_root_by_cancel(client, root_id: str) -> dict:
    """Queue the root, let the runner pick it up, cancel it: the root fails
    with 'cancelled by ...' and the cascade fires (same _fail path as any
    other failure)."""
    client.post(f"/api/v1/stories/{root_id}/queue")
    wait_for_status(client, root_id, "in_progress")
    assert client.post(f"/api/v1/stories/{root_id}/cancel", json={"by": "op"}).status_code == 200
    return wait_for_status(client, root_id, "failed")


def _note_texts(story: dict) -> list[str]:
    return [e["data"] for e in story["events"] if e["kind"] == "note"]


def test_retry_reopens_cascade_dependents(monkeypatch, tmp_path):
    """root -> b(queued) -> c(backlog) -> d(backlog). Cancelling the root
    fails all three with 'dependency failed: root'; retrying the root brings
    every untouched dependent back to the status the cascade took it from,
    error cleared, transitive."""
    client = _reopen_client(monkeypatch, tmp_path, "reopen-retry.db", _HangGateway())
    try:
        root = create_story(client, title="root")
        b = create_story(client, title="b", depends_on=[root["id"]])
        c = create_story(client, title="c", depends_on=[b["id"]])
        d = create_story(client, title="d", depends_on=[c["id"]])
        client.post(f"/api/v1/stories/{b['id']}/queue")

        _fail_root_by_cancel(client, root["id"])
        # the cascade names the DIRECT parent at each level (b failed because
        # of root, c because of b, ...) — which is why the restore matches the
        # prefix, never the title inside it
        expect = {
            b["id"]: ("queued", "dependency failed: root"),
            c["id"]: ("backlog", "dependency failed: b"),
            d["id"]: ("backlog", "dependency failed: c"),
        }
        for sid, (old, error) in expect.items():
            failed = wait_for_status(client, sid, "failed")
            assert failed["error"] == error
            assert old in ("queued", "backlog")  # chain built as documented

        # scheduler HARD-off: the assertions below must not race a fresh run
        client.portal.call(client.app.state.story_runner.stop)

        out = client.post(
            f"/api/v1/stories/{root['id']}/retry", json={"stage": "queued"}
        ).json()
        assert out["status"] == "queued" and out["error"] is None

        restored = {
            sid: client.get(f"/api/v1/stories/{sid}").json()
            for sid in (b["id"], c["id"], d["id"])
        }
        assert restored[b["id"]]["status"] == "queued"
        assert restored[c["id"]]["status"] == "backlog"
        assert restored[d["id"]]["status"] == "backlog"
        for story in restored.values():
            assert story["error"] is None
            # each dependent's note names its own direct parent
            titles = {b["id"]: "root", c["id"]: "b", d["id"]: "c"}
            assert any(
                f"re-opened: {titles[story['id']]} was retried" in t
                for t in _note_texts(story)
            )
        reopened = [
            e["data"]
            for e in restored[b["id"]]["events"]
            if e["kind"] == "status" and '"reopened": true' in e["data"]
        ]
        assert reopened, "the restore must be visible as a status event"
    finally:
        client.__exit__(None, None, None)


def test_requeue_reopens_cascade_dependents(monkeypatch, tmp_path):
    """Same chain, recovered through POST /queue on the failed root instead
    of POST /retry — the second call site of the same helper."""
    client = _reopen_client(monkeypatch, tmp_path, "reopen-queue.db", _HangGateway())
    try:
        root = create_story(client, title="root")
        b = create_story(client, title="b", depends_on=[root["id"]])
        c = create_story(client, title="c", depends_on=[b["id"]])
        d = create_story(client, title="d", depends_on=[c["id"]])
        client.post(f"/api/v1/stories/{b['id']}/queue")

        _fail_root_by_cancel(client, root["id"])
        for sid in (b["id"], c["id"], d["id"]):
            wait_for_status(client, sid, "failed")

        client.portal.call(client.app.state.story_runner.stop)

        out = client.post(f"/api/v1/stories/{root['id']}/queue").json()
        assert out["status"] == "queued"
        assert client.get(f"/api/v1/stories/{b['id']}").json()["status"] == "queued"
        assert client.get(f"/api/v1/stories/{c['id']}").json()["status"] == "backlog"
        assert client.get(f"/api/v1/stories/{d['id']}").json()["status"] == "backlog"
        for sid in (b["id"], c["id"], d["id"]):
            story = client.get(f"/api/v1/stories/{sid}").json()
            assert story["error"] is None
            # each dependent's note names its own direct parent
            titles = {b["id"]: "root", c["id"]: "b", d["id"]: "c"}
            assert any(
                f"re-opened: {titles[sid]} was retried" in t for t in _note_texts(story)
            )
    finally:
        client.__exit__(None, None, None)


def test_patched_dependent_stays_failed_plain_note_restored(
    monkeypatch, tmp_path
):
    """A dependent PATCHed after the cascade is left failed with its error
    intact; a dependent that only gained a POST /notes note (an audit trail,
    not an edit) is restored like an untouched sibling."""
    client = _reopen_client(monkeypatch, tmp_path, "reopen-patched.db", _HangGateway())
    try:
        root = create_story(client, title="root")
        edited = create_story(client, title="edited", depends_on=[root["id"]])
        noted = create_story(client, title="noted", depends_on=[root["id"]])
        sibling = create_story(client, title="sibling", depends_on=[root["id"]])

        _fail_root_by_cancel(client, root["id"])
        for story in (edited, noted, sibling):
            failed = wait_for_status(client, story["id"], "failed")
            assert failed["error"] == "dependency failed: root"

        # an operator edits one dependent, leaves a plain note on another
        patched = client.patch(
            f"/api/v1/stories/{edited['id']}", json={"title": "edited harder"}
        )
        assert patched.status_code == 200
        assert (
            client.post(
                f"/api/v1/stories/{noted['id']}/notes", json={"body": "watching"}
            ).status_code
            == 201
        )

        client.portal.call(client.app.state.story_runner.stop)
        client.post(f"/api/v1/stories/{root['id']}/retry", json={"stage": "queued"})

        stayed = client.get(f"/api/v1/stories/{edited['id']}").json()
        assert stayed["status"] == "failed"
        assert stayed["error"] == "dependency failed: root"
        assert stayed["title"] == "edited harder"
        assert not any("re-opened" in t for t in _note_texts(stayed))

        for story in (noted, sibling):
            restored = client.get(f"/api/v1/stories/{story['id']}").json()
            assert restored["status"] == "backlog"
            assert restored["error"] is None
            assert any("re-opened: root was retried" in t for t in _note_texts(restored))
    finally:
        client.__exit__(None, None, None)


def test_two_parents_restore_only_when_both_retried(monkeypatch, tmp_path):
    """d depends on A and B. Both fail; d's error names A (the first parent
    that failed). Retrying A alone leaves d failed — B still blocks it — and
    retrying B restores it (prefix match, not exact-title)."""
    client = _reopen_client(monkeypatch, tmp_path, "reopen-two-parents.db", _HangGateway())
    try:
        a = create_story(client, title="parent A")
        b = create_story(client, title="parent B")
        d = create_story(client, title="d", depends_on=[a["id"], b["id"]])

        _fail_root_by_cancel(client, a["id"])
        failed = wait_for_status(client, d["id"], "failed")
        assert failed["error"] == "dependency failed: parent A"
        _fail_root_by_cancel(client, b["id"])
        # d already failed: the second cascade must not touch (or rename) it
        assert client.get(f"/api/v1/stories/{d['id']}").json()["error"] == (
            "dependency failed: parent A"
        )

        client.portal.call(client.app.state.story_runner.stop)

        client.post(f"/api/v1/stories/{a['id']}/retry", json={"stage": "queued"})
        still = client.get(f"/api/v1/stories/{d['id']}").json()
        assert still["status"] == "failed"
        assert still["error"] == "dependency failed: parent A"

        client.post(f"/api/v1/stories/{b['id']}/retry", json={"stage": "queued"})
        restored = client.get(f"/api/v1/stories/{d['id']}").json()
        assert restored["status"] == "backlog"
        assert restored["error"] is None
        assert any("re-opened: parent B was retried" in t for t in _note_texts(restored))
    finally:
        client.__exit__(None, None, None)


def test_cascade_from_unexpected_source_status_left_failed(monkeypatch, tmp_path):
    """A dependent whose cascade status event did not come from backlog/queued
    (synthetic, as in test_respec_flow) is never guessed at: it stays failed."""
    from sqlmodel import Session

    from app.models import Story
    from app.runner import record_event

    client = _reopen_client(monkeypatch, tmp_path, "reopen-bad-from.db", _HangGateway())
    try:
        root = create_story(client, title="root")
        dep = create_story(client, title="dep", depends_on=[root["id"]])

        _fail_root_by_cancel(client, root["id"])
        # rewrite the dependent's failure as one from an in-flight status:
        # only the cascade writes that event shape, and it never starts from
        # in_progress — so this story is NOT a cascade victim
        engine = client.app.state.engine
        with Session(engine) as db:
            story = db.get(Story, dep["id"])
            story.status = "failed"
            story.error = "dependency failed: root"
            db.add(story)
            record_event(db, dep["id"], "status", {"from": "in_progress", "to": "failed"})
            db.commit()

        client.portal.call(client.app.state.story_runner.stop)
        client.post(f"/api/v1/stories/{root['id']}/retry", json={"stage": "queued"})

        stayed = client.get(f"/api/v1/stories/{dep['id']}").json()
        assert stayed["status"] == "failed"
        assert stayed["error"] == "dependency failed: root"
        assert not any("re-opened" in t for t in _note_texts(stayed))
    finally:
        client.__exit__(None, None, None)



def test_transient_error_requeues_coder_stage(tmp_path, monkeypatch):
    import subprocess

    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers import Delta, GatewayError, ToolCall

    def init_repo() -> str:
        repo = tmp_path / "project"
        repo.mkdir()
        (repo / "README.md").write_text("# demo\n")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
            cwd=repo,
            check=True,
        )
        return str(repo)

    class CoderFlakyGateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            self.calls += 1
            if self.calls == 1:  # spec
                yield Delta(text=MINIMAL_SPEC)
            elif self.calls == 2:  # ai review of the spec
                yield Delta(text="fine\n\nVERDICT: APPROVED")
            elif self.calls == 3:  # coder attempt 1: connection dies mid-run
                raise GatewayError("APIConnectionError: connection timed out")
            elif self.calls == 4:  # coder retry actually writes
                assert any(t["function"]["name"] == "write_file" for t in tools or [])
                yield ToolCall(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps(
                        {"path": "feature.py", "content": "VALUE = 'x'\n"}
                    ),
                )
            elif self.calls == 5:  # coder final text
                yield Delta(text="wrote feature.py")
            elif self.calls == 6:  # code review
                yield Delta(text="diff matches spec\n\nVERDICT: APPROVED")

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "transient-coder.db"))
    gateway = CoderFlakyGateway()
    app = create_app(gateway=gateway)
    _fast_backoff(monkeypatch)
    with TestClient(app) as c:
        c.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": init_repo(),
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        story = create_story(c)
        c.post(f"/api/v1/stories/{story['id']}/queue")
        wait_for_status(c, story["id"], "human_review", timeout=30)

        # approve the spec -> executing; the coder call dies transiently
        approved = c.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"}).json()
        assert approved["status"] == "executing"

        # requeued from executing, never failed, then finishes the coder run
        deadline = time.time() + 30
        requeued = False
        while time.time() < deadline and not requeued:
            s = c.get(f"/api/v1/stories/{story['id']}").json()
            assert s["status"] != "failed", s.get("error")
            if gateway.calls >= 3 and s["status"] == "queued":
                requeued = True
            time.sleep(0.05)
        assert requeued, "transient coder error never requeued the story"

        final = wait_for_status(c, story["id"], "human_review", timeout=30)
        assert final["base_ref"]

        # the requeue is recorded as a status event carrying a note
        requeue_events = [
            json.loads(e["data"])
            for e in final["events"]
            if "provider error; retry in ~" in e["data"] and "(attempt 1)" in e["data"]
        ]
        assert requeue_events, "expected a transient-error requeue note event"
        assert requeue_events[0]["from"] == "executing"
        assert requeue_events[0]["to"] == "queued"


# ---- story notes (append-only work log) ---------------------------------


def test_story_notes_crud_and_rules(client):
    story = create_story(client)
    ok = client.post(f"/api/v1/stories/{story['id']}/notes", json={"body": "STATE: exploring"})
    assert ok.status_code == 201
    note = ok.json()
    assert note["author"] == "local" and note["body"].startswith("STATE")

    detail = client.get(f"/api/v1/stories/{story['id']}").json()
    assert [n["body"] for n in detail["notes"]] == ["STATE: exploring"]

    second = client.post(
        f"/api/v1/stories/{story['id']}/notes", json={"body": "NEXT: write tests", "author": "mcp"}
    )
    assert second.status_code == 201 and second.json()["author"] == "mcp"
    detail = client.get(f"/api/v1/stories/{story['id']}").json()
    assert [n["author"] for n in detail["notes"]] == ["local", "mcp"]

    kinds = [e["kind"] for e in detail["events"]]
    assert "note" in kinds

    assert client.post("/api/v1/stories/nope/notes", json={"body": "x"}).status_code == 404
    assert (
        client.post(f"/api/v1/stories/{story['id']}/notes", json={"body": "   "}).status_code
        == 400
    )

    # byte cap post-redaction: exactly 8192 -> 201, over -> 400
    exact = client.post(
        f"/api/v1/stories/{story['id']}/notes", json={"body": "文" * 2730 + "aa"}
    )
    assert exact.status_code == 201
    huge = client.post(
        f"/api/v1/stories/{story['id']}/notes", json={"body": "文" * 2731}
    )
    assert huge.status_code == 400

    assert client.patch(f"/api/v1/stories/{story['id']}/notes/1", json={}).status_code in (404, 405)
    assert client.delete(f"/api/v1/stories/{story['id']}/notes/1").status_code in (404, 405)


def test_story_notes_secret_redaction(client):
    story = create_story(client)
    res = client.post(
        f"/api/v1/stories/{story['id']}/notes",
        json={"body": "key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA used"},
    )
    assert res.status_code == 201
    stored = res.json()["body"]
    assert "sk-ant" not in stored and "AAAA" not in stored
    served = client.get(f"/api/v1/stories/{story['id']}").json()["notes"][0]["body"]
    assert served == stored


def test_story_note_on_done_story_allowed(client, gateway):
    story = create_story(client)
    client.post(f"/api/v1/stories/{story['id']}/queue")
    wait_for_status(client, story["id"], "human_review")
    client.patch("/api/v1/settings", json={"write_tools_enabled": False})
    final = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"}).json()
    assert final["status"] == "done"
    client.patch("/api/v1/settings", json={"write_tools_enabled": True})
    note = client.post(f"/api/v1/stories/{story['id']}/notes", json={"body": "retro"})
    assert note.status_code == 201


# ---- provider errors: raw facts recorded, only a real 429 is a rate limit ----


def _run_failing_gateway(monkeypatch, tmp_path, make_error, want_status, *, retries=1):
    """Run one story against a gateway that always raises make_error(); return
    (gateway_calls, final story, status events as dicts)."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    class Gw:
        calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            Gw.calls += 1
            raise make_error()
            yield  # pragma: no cover

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "facts.db"))
    app = create_app(gateway=Gw())
    _fast_backoff(monkeypatch)
    with TestClient(app) as c:
        c.patch(
            "/api/v1/settings",
            json={"story_poll_interval": 0.05, "max_provider_retries": retries},
        )
        story = create_story(c)
        c.post(f"/api/v1/stories/{story['id']}/queue")
        deadline = time.time() + 30
        while True:
            final = c.get(f"/api/v1/stories/{story['id']}").json()
            events = [
                json.loads(e["data"]) for e in final["events"] if e["kind"] == "status"
            ]
            # a requeued story is "queued" before it ever ran: wait for the note
            if final["status"] == want_status and (
                want_status != "queued" or any("retry in ~" in e.get("note", "") for e in events)
            ):
                return Gw.calls, final, events
            assert time.time() < deadline, f"never reached {want_status}: {final['status']}"
            time.sleep(0.05)


def test_real_429_is_rate_limit_with_facts(monkeypatch, tmp_path):
    from app.providers import GatewayError

    def err():
        return GatewayError(
            "[zai] RateLimitError: quota",
            http_status=429,
            error_type="RateLimitError",
            provider_code="1302",
            model="zai/glm-4.6",
            prompt_tokens=1234,
        )

    _calls, final, events = _run_failing_gateway(monkeypatch, tmp_path, err, "queued")
    note = next(e for e in events if "provider rate limit; retry in ~" in e.get("note", ""))
    facts = note["provider"]
    assert facts["classification"] == "rate_limit"
    assert facts["http_status"] == 429
    assert facts["error_type"] == "RateLimitError"
    assert facts["provider_code"] == "1302"
    assert facts["model"] == "zai/glm-4.6"
    assert facts["prompt_tokens"] == 1234


def test_500_with_rate_limit_prose_is_server_error_and_capped(monkeypatch, tmp_path):
    from app.providers import GatewayError

    def err():
        return GatewayError(
            "[zai] InternalServerError: rate limit mentioned in prose, key sk-abcdefghijklmnopqrstuvwxyz123456 "
            + "x" * 900,
            http_status=500,
            error_type="InternalServerError",
            model="zai/glm-4.6",
        )

    _calls, final, events = _run_failing_gateway(monkeypatch, tmp_path, err, "failed")
    notes = [e for e in events if "retry in ~" in e.get("note", "")]
    assert len(notes) == 1  # capped at max_provider_retries=1
    assert "provider server error; retry in ~" in notes[0]["note"]
    facts = notes[0]["provider"]
    assert facts["classification"] == "server_error"
    assert facts["http_status"] == 500
    assert len(facts["message"]) <= 500
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in facts["message"]
    failed = events[-1]
    assert failed["to"] == "failed"
    assert failed["provider"]["classification"] == "server_error"


def test_timeout_requeues_capped_with_timeout_wording(monkeypatch, tmp_path):
    from app.providers import GatewayError

    def err():
        return GatewayError(
            "[zai] APITimeoutError: Request timed out",
            error_type="APITimeoutError",
            model="zai/glm-4.6",
        )

    _calls, final, events = _run_failing_gateway(monkeypatch, tmp_path, err, "failed")
    notes = [e for e in events if "retry in ~" in e.get("note", "")]
    assert len(notes) == 1
    assert "provider timeout; retry in ~" in notes[0]["note"]
    assert notes[0]["provider"]["classification"] == "timeout"
    assert notes[0]["provider"]["error_type"] == "APITimeoutError"


def test_context_too_large_fails_immediately_without_retry(monkeypatch, tmp_path):
    from app.providers import GatewayError

    def err():
        return GatewayError(
            "[zai] ContextWindowExceededError: maximum context length is 200000",
            http_status=400,
            error_type="ContextWindowExceededError",
            provider_code="context_length_exceeded",
            model="zai/glm-4.6",
            prompt_tokens=250000,
        )

    calls, final, events = _run_failing_gateway(monkeypatch, tmp_path, err, "failed")
    assert calls == 1
    assert final["error"].startswith("context too large:")
    assert not any("retry in ~" in e.get("note", "") for e in events)
    facts = events[-1]["provider"]
    assert facts["classification"] == "context_too_large"
    assert facts["http_status"] == 400
    assert facts["prompt_tokens"] == 250000


def test_string_only_error_still_requeues_as_legacy():
    from app.providers import GatewayError
    from app.runner import classify_provider_error

    assert classify_provider_error(GatewayError("[zai] RateLimitError: 1302 rate limit")) == "rate_limit"
    assert classify_provider_error(GatewayError("[zai] error 429 too many")) == "rate_limit"
    assert classify_provider_error(GatewayError("got 14290 items")) == "hard"
    assert classify_provider_error(GatewayError("[zai] InternalServerError: boom")) == "server_error"
    assert classify_provider_error(GatewayError("[zai] MidStreamFallbackError: x")) == "timeout"
    assert classify_provider_error(GatewayError("[zai] AuthenticationError: 401")) == "infra"
    assert classify_provider_error(GatewayError("[x] prompt is too long")) == "context_too_large"
