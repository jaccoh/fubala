"""Guardrails: deny-lists, secret redaction, and budgets."""

import asyncio
import json
import time
from pathlib import Path

import pytest

from app.agent import run_agent
from app.agent_tools import ToolBox
from app.guardrails import GuardedExecutor, ToolPolicy, path_denied, redact_secrets
from app.providers import Delta, ToolCall, Usage


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".env").write_text("API_KEY=sk-abc123def456ghi789jkl012\nDB=postgres")
    (tmp_path / "app.py").write_text('token = "ghp_' + "a" * 40 + '"\nprint("hi")\n')
    (tmp_path / "notes.md").write_text("nothing secret here")
    return tmp_path


# ---- static helpers ------------------------------------------------------


def test_redact_secrets_catches_common_shapes():
    text = "sk-ant-abc123def456ghi789jkl012 ghp_" + "a" * 40 + " AKIAIOSFODNN7EXAMPLE"
    red = redact_secrets(text)
    assert "sk-ant-abc" not in red and "ghp_" not in red and "AKIA" not in red
    assert "REDACTED" in red


def test_path_denied_defaults_and_globs():
    assert path_denied(".env") and path_denied("config/server.pem")
    assert path_denied("private.key")
    assert not path_denied("main.py")
    assert path_denied("Specs.MD", extra_globs=("*.md",))


def test_denied_files_are_unreachable(project):
    box = ToolBox(engine=None, project_root=str(project))
    listing = box.execute("list_files", "{}")
    assert ".env" not in listing and "app.py" in listing

    denied = box.execute("read_file", json.dumps({"path": ".env"}))
    assert "error" in denied and "postgres" not in denied

    hits = box.execute("search_files", json.dumps({"query": "postgres"}))
    assert hits == "(no matches)"


def test_secrets_redacted_from_tool_output(project):
    import asyncio

    box = ToolBox(engine=None, project_root=str(project))
    guarded = GuardedExecutor(box, ToolPolicy())
    result = asyncio.run(guarded("read_file", json.dumps({"path": "app.py"})))
    assert "ghp_" not in result
    assert "REDACTED" in result
    assert result.startswith("[tool output")


def test_operator_globs_extend_deny_list(project):
    policy = ToolPolicy(deny_globs=["*.md"])
    box = ToolBox(engine=None, project_root=str(project), deny_globs=policy.denied_patterns())
    result = box.execute("read_file", json.dumps({"path": "notes.md"}))
    assert "error" in result


# ---- budgets ---------------------------------------------------------------


def test_call_budget_exhaustion(project):
    import asyncio

    box = ToolBox(engine=None, project_root=str(project))
    guarded = GuardedExecutor(box, ToolPolicy(max_tool_calls=1))
    first = asyncio.run(guarded("read_file", json.dumps({"path": "app.py"})))
    assert "error" not in first
    second = asyncio.run(guarded("read_file", json.dumps({"path": "notes.md"})))
    assert "budget exhausted" in second
    assert guarded.exhausted


def test_time_budget_exhaustion(project):
    import asyncio

    box = ToolBox(engine=None, project_root=str(project))
    guarded = GuardedExecutor(box, ToolPolicy(timeout_seconds=0.0))
    result = asyncio.run(guarded("read_file", json.dumps({"path": "app.py"})))
    assert "time budget exhausted" in result


def test_reset_restores_call_and_time_budget(project):
    """AC3: reset() gives the executor a fresh round — calls back to zero,
    deadline pushed forward — while a single round still refuses the
    (max_tool_calls + 1)-th call."""
    import asyncio

    box = ToolBox(engine=None, project_root=str(project))
    guarded = GuardedExecutor(box, ToolPolicy(max_tool_calls=1, timeout_seconds=30))

    # round 1: the whole call budget is spent on one call
    first = asyncio.run(guarded("read_file", json.dumps({"path": "app.py"})))
    assert "error" not in first
    second = asyncio.run(guarded("read_file", json.dumps({"path": "app.py"})))
    assert "tool call budget exhausted" in second
    assert guarded.exhausted

    # reset(): new round, full budget again
    guarded.reset()
    assert guarded.calls == 0
    after = asyncio.run(guarded("read_file", json.dumps({"path": "notes.md"})))
    assert "error" not in after and after.startswith("[tool output")

    # ...but the round's own limit still bites immediately after
    overrun = asyncio.run(guarded("read_file", json.dumps({"path": "app.py"})))
    assert "tool call budget exhausted" in overrun


def test_reset_pushes_deadline_forward(project):
    """reset() on an expired deadline makes an immediate call succeed, and
    the new deadline is strictly later than the expired one."""
    import asyncio

    box = ToolBox(engine=None, project_root=str(project))
    guarded = GuardedExecutor(box, ToolPolicy(timeout_seconds=0.2))
    before = guarded.deadline
    time.sleep(0.25)  # let the round's clock run out
    assert "time budget exhausted" in asyncio.run(
        guarded("read_file", json.dumps({"path": "app.py"}))
    )
    guarded.reset()
    assert guarded.calls == 0
    assert guarded.deadline > before
    result = asyncio.run(guarded("read_file", json.dumps({"path": "app.py"})))
    assert "error" not in result and result.startswith("[tool output")


def test_reset_keeps_operator_approvals(project):
    """AC4: reset() only rewinds the counters. A root the operator approved in
    round 1 (toolbox.allowed_roots) stays readable in the fix round —
    reset() must never hand the permissions back."""
    import asyncio
    import tempfile

    # a directory genuinely outside the project root (the `project` fixture
    # IS tmp_path, so a sibling subdir would still be inside)
    outside = Path(tempfile.mkdtemp(prefix="fubala-outside-"))
    (outside / "notes.md").write_text("outside the project\n")

    box = ToolBox(engine=None, project_root=str(project))
    guarded = GuardedExecutor(box, ToolPolicy(max_tool_calls=2))

    # round 1: the read is refused (outside the roots)…
    denied = asyncio.run(
        guarded("read_file", json.dumps({"path": str(outside / "notes.md")}))
    )
    assert "error" in denied and "request_permission" in denied

    # …the operator approves it — exactly what _handle_permission_request does
    box.allowed_roots.append((outside / "notes.md").resolve())
    read = asyncio.run(
        guarded("read_file", json.dumps({"path": str(outside / "notes.md")}))
    )
    assert "outside the project" in read and read.startswith("[tool output")
    assert guarded.calls == 2  # round 1's budget fully spent

    # fix round: fresh counters, approval intact
    guarded.reset()
    assert guarded.calls == 0
    again = asyncio.run(
        guarded("read_file", json.dumps({"path": str(outside / "notes.md")}))
    )
    assert "outside the project" in again, "approval lost across the reset"
    assert again.startswith("[tool output")


def test_request_permission_still_counts_toward_calls(project):
    import asyncio

    box = ToolBox(engine=None, project_root=str(project))
    guarded = GuardedExecutor(box, ToolPolicy(max_tool_calls=1, permission_mode="strict"))
    first = asyncio.run(
        guarded("request_permission", json.dumps({"path": "/tmp", "reason": "r"}))
    )
    assert "strict mode" in first
    assert guarded.calls == 1
    second = asyncio.run(guarded("read_file", json.dumps({"path": "app.py"})))
    assert "tool call budget exhausted" in second


def test_run_agent_drops_tools_when_budget_spent(project):
    class GreedyGateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            self.calls += 1
            if self.calls == 1:
                yield ToolCall(id="c1", name="read_file", arguments='{"path": "app.py"}')
                yield Usage(prompt_tokens=1, completion_tokens=1)
            else:
                # after the budget the model must be offered NO tools
                assert tools is None, "tools must be withdrawn once the budget is spent"
                yield Delta(text="final answer without tools")
                yield Usage(prompt_tokens=2, completion_tokens=3)

    box = ToolBox(engine=None, project_root=str(project))
    guarded = GuardedExecutor(box, ToolPolicy(max_tool_calls=1))

    async def go():
        return await run_agent(
            GreedyGateway(),
            model="echo/dev",
            messages=[{"role": "user", "content": "go"}],
            tools=box.schemas(),
            execute=guarded,
            max_tool_calls=1,
        )

    result = asyncio.run(go())
    assert result.text == "final answer without tools"
    assert result.rounds == 2 and result.tool_calls == 1


# ---- stage toggles ---------------------------------------------------------


def test_chat_tools_can_be_disabled(client, tmp_path):
    client.patch("/api/v1/settings", json={"project_root": str(tmp_path)})
    client.patch("/api/v1/settings", json={"chat_tools_enabled": False})
    sid = client.post("/api/v1/chat/sessions", json={}).json()["id"]
    res = client.post(f"/api/v1/chat/sessions/{sid}/messages", json={"content": "hi"})

    from conftest import parse_sse

    start = dict(parse_sse(res.text))["start"]
    assert start["tools"] == []


def test_spec_tools_can_be_disabled(client, monkeypatch, tmp_path):
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "spec.db"))
    from fastapi.testclient import TestClient
    from test_stories import SequencedGateway

    from app.main import create_app

    gateway = SequencedGateway([["## Goal\nspec"], ["VERDICT: APPROVED"]])
    with TestClient(create_app(gateway=gateway)) as c:
        c.patch("/api/v1/settings", json={"story_poll_interval": 0.05, "spec_tools_enabled": False})
        story = c.post("/api/v1/stories", json={"title": "t"}).json()
        c.post(f"/api/v1/stories/{story['id']}/queue")

        import time

        deadline = time.time() + 30
        while time.time() < deadline:
            if c.get(f"/api/v1/stories/{story['id']}").json()["status"] in (
                "human_review", "failed", "done"
            ):
                break
            time.sleep(0.05)
        # the spec call was made with NO tools offered
        assert gateway.calls[0]["tools"] is None
