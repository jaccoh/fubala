"""Regression coverage for the code-gate no-op reject path (AC3: reviewer
override + requeue), kept here so the behaviour stays pinned after the
scratch harness that drove its development was removed."""

import json

from fastapi.testclient import TestClient

from app.main import create_app

from test_execution import (
    ReasonedRetryGW,
    _takein_story_to_review_gate,
    _takein_wait_terminal,
    init_repo,
)


def test_code_review_override_requeues_and_recovers(tmp_path, monkeypatch, legacy_review):
    """A code reviewer who approves while explicitly leaving a human reject
    reason NOT ADDRESSED is overridden to rejected and the story requeued;
    the next round addresses it and reaches the human gate."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "crrev1.db"))

    class ReviewGW(ReasonedRetryGW):
        def __init__(self):
            super().__init__()
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
                        yield D(
                            text="Reason 1: NOT ADDRESSED — no logging was added\n\n"
                            "VERDICT: APPROVED"
                        )
                    else:
                        yield D(text="Reason 1: ADDRESSED\n\nVERDICT: APPROVED")
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

    gw = ReviewGW()
    with TestClient(create_app(gateway=gw)) as client:
        sid = _takein_story_to_review_gate(
            client, repo, "review reasons", verify_command=""
        )
        pre_prompts = len(gw.prompts)
        pre_events = len(client.get(f"/api/v1/stories/{sid}").json()["events"])
        client.post(
            f"/api/v1/stories/{sid}/reject",
            json={"reason": "needs more logging and a docstring"},
        )

        final = _takein_wait_terminal(client, sid, timeout=60)
        assert final["status"] == "human_review", final.get("error")
        detail = client.get(f"/api/v1/stories/{sid}").json()
        after = detail["events"][pre_events:]
        ai_verdicts = [
            json.loads(e["data"])["verdict"]
            for e in after
            if e["kind"] == "ai_review"
        ]
        assert "rejected" in ai_verdicts, (
            f"an approving verdict over an unaddressed reason was not "
            f"overridden to rejected: {ai_verdicts}"
        )
        requeued = [
            json.loads(e["data"])
            for e in after
            if e["kind"] == "status" and json.loads(e["data"]).get("to") == "queued"
        ]
        assert any(r.get("from") == "ai_review" for r in requeued), (
            f"the story was never requeued from ai_review: {requeued}"
        )
        assert final["status"] != "failed"

        review_prompts = [
            p for p in gw.prompts[pre_prompts:]
            if p.startswith("=== CODE REVIEW REQUEST")
        ]
        assert review_prompts, "code review never ran"
        assert "## Human reject reasons to verify" in review_prompts[0]
        assert "1. needs more logging and a docstring" in review_prompts[0]
        assert "NOT ADDRESSED" in review_prompts[0]
        assert "Reason N: ADDRESSED" in review_prompts[0]
