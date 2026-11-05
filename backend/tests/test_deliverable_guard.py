"""Phase deliverable guard: a thinking-only ending must not turn stale
narration into the spec.

Regression suite for the empty-round exhaustion path in run_agent and the
section validation in Runner._generate_spec.
"""

import json
import time

import pytest


def create_story(client, **body) -> dict:
    res = client.post("/api/v1/stories", json={"title": "Guard story", **body})
    assert res.status_code == 201
    return res.json()


CAP_SPEC = "## Goal\nx\n## Implementation steps\n1. y\n## Acceptance checks\n- [ ] z\n"


def test_agent_cap_setting_clamps_escalation_ceiling():
    """agent_max_output_tokens_cap (settings) clamps how far a cap-hit
    escalation may climb; it can never raise the built-in 32768 ceiling."""
    gateway = ScriptedGateway(
        [
            {"deltas": [], "usage": _usage(8192, "length")},
            {"deltas": [], "usage": _usage(12288, "length")},
            {"deltas": [], "usage": _usage(12288, "length")},
            {"deltas": [], "usage": _usage(12288, "length")},
            {"deltas": [], "usage": _usage(12288, "length")},
            {"deltas": [], "usage": _usage(12288, "length")},
            {"deltas": [], "usage": _usage(12288, "length")},
        ]
    )
    result = _run(gateway, max_output_tokens=8192, max_output_tokens_cap=12288)

    budgets = [c["max_output_tokens"] for c in gateway.calls]
    assert budgets[0] == 8192
    assert budgets[1] == 12288, "escalation doubles but is clamped to the cap"
    assert all(b == 12288 for b in budgets[1:])
    assert result.complete is False
    assert result.text == ""


def test_agent_cap_setting_higher_than_builtin_is_ignored():
    """A cap above the built-in 32768 ceiling must not raise the ceiling."""
    gateway = ScriptedGateway(
        [
            {"deltas": [], "usage": _usage(32768, "length")},
            {"deltas": [], "usage": _usage(32768, "length")},
            {"deltas": [], "usage": _usage(32768, "length")},
            {"deltas": [], "usage": _usage(32768, "length")},
            {"deltas": [], "usage": _usage(32768, "length")},
            {"deltas": [], "usage": _usage(32768, "length")},
        ]
    )
    _run(gateway, max_output_tokens=32768, max_output_tokens_cap=131072)

    budgets = [c["max_output_tokens"] for c in gateway.calls]
    assert budgets == [32768, 32768, 32768, 32768, 32768, 32768]


def wait_for_status(client, story_id: str, want: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    story = None
    while time.time() < deadline:
        story = client.get(f"/api/v1/stories/{story_id}").json()
        if story["status"] == want:
            return story
        time.sleep(0.05)
    pytest.fail(f"story never reached {want}; last status: {story and story['status']}")


# ---- fakes ---------------------------------------------------------------


class ScriptedGateway:
    """Replays a list of scripted rounds; records every stream_chat call.

    Each round is a dict:
        deltas:  list[str]          streamed text (optional)
        calls:   list[ToolCall]     tool calls (optional)
        usage:   Usage              usage event (optional)
    """

    def __init__(self, rounds: list[dict]):
        self.rounds = [dict(r) for r in rounds]
        self.calls: list[dict] = []

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
        from app.providers import Delta, ToolCall, Usage

        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "tools": tools,
                "max_output_tokens": max_output_tokens,
            }
        )
        index = min(len(self.calls) - 1, len(self.rounds) - 1)
        round_ = self.rounds[index]
        for chunk in round_.get("deltas", []):
            yield Delta(text=chunk)
        for call in round_.get("calls", []):
            yield call
        if round_.get("usage"):
            yield round_["usage"]
        elif "usage" not in round_:
            yield Usage(prompt_tokens=1, completion_tokens=1)


def _echo(name: str, arguments: str = "{}") -> object:
    from app.providers import ToolCall

    return ToolCall(id=f"call_{name}", name=name, arguments=arguments)


def _usage(completion_tokens: int = 1, finish_reason: str | None = None) -> object:
    from app.providers import Usage

    return Usage(
        prompt_tokens=5, completion_tokens=completion_tokens, finish_reason=finish_reason
    )


def _run(gateway, messages=None, tools=None, execute=None, **kwargs):
    import asyncio

    from app.agent import run_agent

    return asyncio.run(
        run_agent(
            gateway,
            model="echo/dev",
            messages=messages or [{"role": "user", "content": "spec it"}],
            tools=tools,
            execute=execute,
            **kwargs,
        )
    )


def _executor(results: dict[str, str] | None = None):
    results = results or {}

    def execute(name: str, arguments: str) -> str:
        return results.get(name, "ok")

    return execute


# ---- step 1: red-first — stale narration must not become the deliverable


def test_empty_round_exhaustion_is_incomplete_and_not_narration():
    """2 tool rounds (the 2nd carrying narration), then 4 silent rounds: the
    run must end incomplete with empty text — never the round-2 narration."""
    gateway = ScriptedGateway(
        [
            {"calls": [_echo("list_files")], "usage": _usage(4)},
            {
                "deltas": ["Working on it, reading the files first..."],
                "calls": [_echo("read_file", '{"path": "README.md"}')],
                "usage": _usage(20),
            },
            {"deltas": ["[System: thinking]"], "usage": _usage(900)},
            {"deltas": [], "usage": _usage(900)},
            {"deltas": ["[System: thinking]"], "usage": _usage(900)},
            {"deltas": [], "usage": _usage(900)},
            {"deltas": [], "usage": _usage(900)},
            {"deltas": [], "usage": _usage(900)},
        ]
    )
    result = _run(gateway, tools=[{"fake": True}], execute=_executor())

    assert result.complete is False
    assert result.text == ""
    assert "Working on it" not in result.text
    assert result.tool_calls == 2
    # rounds 3-5 each got a nudge; round 6 exhausted the patience budget
    assert len(gateway.calls) == 6
    nudges = [
        m
        for m in gateway.calls[-1]["messages"]
        if m.get("role") == "user" and "no output" in (m.get("content") or "")
    ]
    assert len(nudges) == 3


def test_max_rounds_without_final_text_is_incomplete():
    """A tool-only run that hits MAX_ROUNDS without ever writing a final
    answer is not a deliverable either."""
    from app.agent import MAX_ROUNDS

    gateway = ScriptedGateway(
        [
            {"calls": [_echo("list_files")], "usage": _usage(10)},
        ]
    )
    result = _run(gateway, tools=[{"fake": True}], execute=_executor())

    assert result.rounds == MAX_ROUNDS
    assert result.complete is False
    assert result.text == ""


def test_max_rounds_ending_in_text_is_complete():
    """MAX_ROUNDS reached with the final round carrying real output: the
    narration IS the deliverable, so the run counts as complete."""
    from app.agent import MAX_ROUNDS

    gateway = ScriptedGateway(
        [
            {
                "deltas": ["## Goal\nfinal answer"],
                "calls": [_echo("list_files")],
                "usage": _usage(10),
            },
        ]
    )
    result = _run(gateway, tools=[{"fake": True}], execute=_executor())

    assert result.rounds == MAX_ROUNDS
    assert result.complete is True
    assert result.text == "## Goal\nfinal answer"


def test_normal_answer_stays_complete():
    """Tool round then a real answer: complete, unchanged behaviour."""
    gateway = ScriptedGateway(
        [
            {"calls": [_echo("list_files")], "usage": _usage(4)},
            {"deltas": ["## Goal\n", "do the thing"], "usage": _usage(30)},
        ]
    )
    result = _run(gateway, tools=[{"fake": True}], execute=_executor())

    assert result.complete is True
    assert result.text == "## Goal\ndo the thing"


# ---- step 3: Usage.finish_reason + max_output_tokens plumbing


def test_usage_carries_finish_reason_and_gateway_receives_budget():
    from app.providers import Usage

    u = Usage(prompt_tokens=1, completion_tokens=2)
    assert u.finish_reason is None
    u2 = Usage(prompt_tokens=1, completion_tokens=2, finish_reason="length")
    assert u2.finish_reason == "length"

    gateway = ScriptedGateway(
        [
            {"deltas": ["hello"], "usage": _usage(3, "stop")},
        ]
    )
    result = _run(gateway, max_output_tokens=4096)
    assert result.complete is True
    assert gateway.calls[0]["max_output_tokens"] == 4096
    assert result.usage.finish_reason == "stop"


# ---- step 4: cap hit -> one escalation, then the ordinary nudge path


def test_cap_hit_escalates_budget_once_then_nudges():
    """finish_reason length with no text: budget doubles (capped), max 2
    escalations, then the empty-round nudge path takes over."""
    gateway = ScriptedGateway(
        [
            {"deltas": [], "usage": _usage(8192, "length")},
            {"deltas": [], "usage": _usage(16384, "length")},
            {"deltas": [], "usage": _usage(32768, "length")},
            {"deltas": [], "usage": _usage(32768, "length")},
            {"deltas": [], "usage": _usage(32768, "length")},
            {"deltas": [], "usage": _usage(32768, "length")},
            {"deltas": [], "usage": _usage(32768, "length")},
        ]
    )
    result = _run(gateway, max_output_tokens=8192)

    budgets = [c["max_output_tokens"] for c in gateway.calls]
    # escalations: 8192 -> 16384 -> 32768, ceiling holds afterwards
    assert budgets[:4] == [8192, 16384, 32768, 32768]
    assert result.complete is False
    assert result.text == ""
    # nudge messages exist after the escalation path gives up
    nudges = [
        m
        for c in gateway.calls
        for m in c["messages"]
        if m.get("role") == "user" and "no output" in (m.get("content") or "")
    ]
    assert nudges


def test_completion_tokens_at_cap_counts_as_cap_hit():
    """No finish_reason, but completion tokens == the budget: still a cap hit."""
    gateway = ScriptedGateway(
        [
            {"deltas": [], "usage": _usage(8192)},
            {"deltas": ["## Goal\nfinal text"], "usage": _usage(120)},
        ]
    )
    result = _run(gateway, max_output_tokens=8192)

    assert [c["max_output_tokens"] for c in gateway.calls] == [8192, 16384]
    assert result.complete is True
    assert result.text == "## Goal\nfinal text"


def test_cap_hit_escalation_message_tells_model_to_continue():
    gateway = ScriptedGateway(
        [
            {"deltas": [], "usage": _usage(8192, "length")},
            {"deltas": ["## Goal\nrecovered"], "usage": _usage(100, "stop")},
        ]
    )
    _run(gateway, max_output_tokens=8192)

    escalation_msgs = [
        m
        for c in gateway.calls
        for m in c["messages"]
        if m.get("role") == "user" and "output token limit" in (m.get("content") or "")
    ]
    assert escalation_msgs, "escalation must nudge the model with a message"


# ---- step 6: _generate_spec validation, nudge, and failure


VALID_SPEC = (
    "## Goal\nAdd the guard.\n\n"
    "## Context\nThe agent loop caches narration.\n\n"
    "## Implementation steps\n1. Fix the loop\n2. Add tests\n\n"
    "## Risks & edge cases\n- Signature changes\n\n"
    "## Acceptance checks\n- [ ] tests pass\n"
)


def _app_with(gateway, monkeypatch, tmp_path, name: str):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / f"{name}.db"))
    client = TestClient(create_app(gateway=gateway))
    client.__enter__()
    client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
    return client


def test_spec_sections_helper():
    from app.runner import REQUIRED_SPEC_SECTIONS, _spec_sections_ok

    assert set(REQUIRED_SPEC_SECTIONS) == {
        "## Goal",
        "## Implementation steps",
        "## Acceptance checks",
    }
    assert _spec_sections_ok(VALID_SPEC)
    assert _spec_sections_ok("## goal\nlower\n## implementation steps\nx\n## acceptance checks")
    assert not _spec_sections_ok("## Goal\nno steps")
    assert not _spec_sections_ok("")
    assert not _spec_sections_ok(None)
    assert not _spec_sections_ok("[System: thinking]")


def test_runner_fails_story_when_spec_phase_is_silent(tmp_path, monkeypatch):
    """The core regression: silent spec phase → FAILED with 'spec phase
    produced no spec', and the story NEVER reaches AI review."""
    gateway = ScriptedGateway(
        [
            {"calls": [_echo("list_files")], "usage": _usage(4)},
            {
                "deltas": ["Reading the repo to understand the task..."],
                "calls": [_echo("read_file", '{"path": "README.md"}')],
                "usage": _usage(20),
            },
            {"deltas": ["[System: thinking]"], "usage": _usage(900)},
            {"deltas": [], "usage": _usage(900)},
            {"deltas": ["[System: thinking]"], "usage": _usage(900)},
            {"deltas": [], "usage": _usage(900)},
            {"deltas": [], "usage": _usage(900)},
            {"deltas": [], "usage": _usage(900)},
        ]
    )
    client = _app_with(gateway, monkeypatch, tmp_path, "silent-spec")
    story = create_story(client)
    client.post(f"/api/v1/stories/{story['id']}/queue")

    failed = wait_for_status(client, story["id"], "failed")
    assert failed["error"] == "spec phase produced no spec"
    assert not failed["spec"]

    events = client.get(f"/api/v1/stories/{story['id']}").json()["events"]
    # no AI-review event: the story was never handed off for review
    assert not [e for e in events if e["kind"] == "ai_review"]
    statuses = [
        json.loads(e["data"]).get("to")
        for e in events
        if e["kind"] == "status"
    ]
    assert "ai_review" not in statuses


def test_sectionless_spec_gets_one_corrective_nudge_then_accepts(tmp_path, monkeypatch):
    """Round 1 narrates instead of speccing; the nudge round produces a valid
    spec → accepted (AI_REVIEW path continues to human_review)."""
    gateway = ScriptedGateway(
        [
            {"deltas": ["Let me think about this story."], "usage": _usage(50)},
            {"deltas": [VALID_SPEC], "usage": _usage(400)},
            {"deltas": ["approved\n\nVERDICT: APPROVED"], "usage": _usage(50)},
        ]
    )
    client = _app_with(gateway, monkeypatch, tmp_path, "nudge-accept")
    story = create_story(client)
    client.post(f"/api/v1/stories/{story['id']}/queue")

    done = wait_for_status(client, story["id"], "human_review")
    assert "## Goal" in done["spec"]

    # exactly one corrective nudge was sent
    corrective = [
        m
        for c in gateway.calls
        for m in c["messages"]
        if m.get("role") == "user" and "not a usable spec" in (m.get("content") or "")
    ]
    assert len(corrective) == 1


def test_sectionless_spec_still_bad_after_nudge_fails(tmp_path, monkeypatch):
    gateway = ScriptedGateway(
        [
            {"deltas": ["thinking aloud, no sections"], "usage": _usage(50)},
            {"deltas": ["still narrating"], "usage": _usage(50)},
        ]
    )
    client = _app_with(gateway, monkeypatch, tmp_path, "nudge-fail")
    story = create_story(client)
    client.post(f"/api/v1/stories/{story['id']}/queue")

    failed = wait_for_status(client, story["id"], "failed")
    assert failed["error"] == "spec phase produced no spec"
    assert len(gateway.calls) == 2, "exactly one corrective nudge, no third call"


def test_valid_spec_first_try_has_no_nudge(tmp_path, monkeypatch):
    gateway = ScriptedGateway(
        [
            {"deltas": [VALID_SPEC], "usage": _usage(400)},
        ]
    )
    client = _app_with(gateway, monkeypatch, tmp_path, "happy-spec")
    story = create_story(client)
    client.post(f"/api/v1/stories/{story['id']}/queue")

    done = wait_for_status(client, story["id"], "human_review")
    assert done["spec"] == VALID_SPEC
    assert len(gateway.calls) == 2  # spec + ai review, no corrective round


def test_spec_budget_comes_from_settings(tmp_path, monkeypatch):
    gateway = ScriptedGateway([{"deltas": [VALID_SPEC], "usage": _usage(400)}])
    client = _app_with(gateway, monkeypatch, tmp_path, "budget-settings")
    client.patch("/api/v1/settings", json={"spec_max_output_tokens": 2048})
    story = create_story(client)
    client.post(f"/api/v1/stories/{story['id']}/queue")

    wait_for_status(client, story["id"], "human_review")
    assert gateway.calls[0]["max_output_tokens"] == 2048


# ---- coordinator gate fixes (16 Oct) --------------------------------------


def _init_repo(path) -> str:
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("# demo\n")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        cwd=path,
        check=True,
    )
    return str(path)


def test_spec_budget_defaults_to_the_gateway_round_budget(tmp_path, monkeypatch):
    """Gate fix 1: with default settings the spec phase requests
    Settings.max_output_tokens (the budget the gateway already sends), never a
    smaller hard-coded default that would halve every round."""
    from app.config import get_settings

    gateway = ScriptedGateway([{"deltas": [VALID_SPEC], "usage": _usage(400)}])
    client = _app_with(gateway, monkeypatch, tmp_path, "budget-default")
    story = create_story(client)
    client.post(f"/api/v1/stories/{story['id']}/queue")

    wait_for_status(client, story["id"], "human_review")
    assert gateway.calls[0]["max_output_tokens"] == get_settings().max_output_tokens


def test_coder_budget_defaults_to_the_gateway_round_budget_and_follows_its_setting(
    tmp_path, monkeypatch
):
    """Gate fix 1 for the coder phase: default = Settings.max_output_tokens;
    coder_max_output_tokens set = that value."""
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import create_app
    from app.providers import ToolCall

    for name, setting, want in (
        ("coder-default", None, get_settings().max_output_tokens),
        ("coder-set", 4096, 4096),
    ):
        repo = _init_repo(tmp_path / name)
        gateway = ScriptedGateway(
            [
                {"deltas": [VALID_SPEC]},  # spec
                {"deltas": ["VERDICT: APPROVED"]},  # spec AI review
                {  # coder round 1: one write
                    "calls": [
                        ToolCall(
                            id="w1",
                            name="write_file",
                            arguments=json.dumps({"path": "x.py", "content": "x = 1\n"}),
                        )
                    ]
                },
                {"deltas": ["done"]},  # coder round 2: final text
                {"deltas": ["VERDICT: APPROVED"]},  # code review
            ]
        )
        monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / f"{name}.db"))
        with TestClient(create_app(gateway=gateway)) as client:
            body = {
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "verify_command": "true",
            }
            if setting is not None:
                body["coder_max_output_tokens"] = setting
            assert client.patch("/api/v1/settings", json=body).status_code == 200
            story = create_story(client)
            client.post(f"/api/v1/stories/{story['id']}/queue")
            wait_for_status(client, story["id"], "human_review", timeout=30)
            # approve the spec so the coder runs, then wait for the code gate
            client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
            deadline = time.time() + 30
            while time.time() < deadline:
                current = client.get(f"/api/v1/stories/{story['id']}").json()
                if current["status"] in ("human_review", "failed", "done"):
                    break
                time.sleep(0.05)
            assert current["status"] == "human_review", current.get("error")

        coder_calls = gateway.calls[2:4]
        assert len(coder_calls) == 2, [c["messages"][-1] for c in gateway.calls]
        assert all(c["max_output_tokens"] == want for c in coder_calls), (
            name,
            [c["max_output_tokens"] for c in coder_calls],
        )


def test_info_story_without_spec_sections_reaches_review(tmp_path, monkeypatch):
    """Gate fix 2: an info story's deliverable is the finished document, which
    has no implementation steps -- the section check is for code stories only.
    (A code story with section-less text still gets the nudge: see
    test_sectionless_spec_gets_one_corrective_nudge_then_accepts.)"""
    doc = "# Findings\n\nThe answer is 42; the table below shows why.\n"
    gateway = ScriptedGateway(
        [
            {"deltas": [doc], "usage": _usage(400)},
            {"deltas": ["fine\n\nVERDICT: APPROVED"], "usage": _usage(50)},
        ]
    )
    client = _app_with(gateway, monkeypatch, tmp_path, "info-doc")
    story = create_story(client, kind="info")
    client.post(f"/api/v1/stories/{story['id']}/queue")

    done = wait_for_status(client, story["id"], "human_review")
    assert done["spec"] == doc
    nudges = [
        m
        for c in gateway.calls
        for m in c["messages"]
        if m.get("role") == "user" and "not a usable spec" in (m.get("content") or "")
    ]
    assert not nudges
