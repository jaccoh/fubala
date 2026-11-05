"""Per-story coder tool budget (story: allow tools_max_calls above 64).

The app setting `tools_max_calls` accepts 1..160 (schema + round cap raised
together), and a story may override its per-run coder budget via
`max_tool_calls` (create, update, spec-gate approve). None = inherit the app
setting; 0 is rejected, never read as "unlimited". Spec/chat runs keep the
app default.
"""

import json
import subprocess
import time

from fastapi.testclient import TestClient

from app.main import create_app
from app.runner import ACTIVITY_KINDS

from conftest import MINIMAL_SPEC


def init_repo(tmp_path) -> str:
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "README.md").write_text("# demo repo\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return str(repo)


def wait_for_status(client: TestClient, story_id: str, statuses: tuple[str, ...], timeout: float = 30):
    deadline = time.time() + timeout
    cur = None
    while time.time() < deadline:
        cur = client.get(f"/api/v1/stories/{story_id}").json()
        if cur["status"] in statuses:
            return cur
        time.sleep(0.05)
    raise AssertionError(f"story never reached {statuses}: {cur and cur.get('error')}")


def activity_from(detail: dict) -> list[tuple[str, dict]]:
    out = []
    for ev in detail["events"]:
        if ev["kind"] in ACTIVITY_KINDS:
            out.append((ev["kind"], json.loads(ev["data"])))
    return out


class TwoToolCoderGateway:
    """spec -> ai review approve -> coder (two tool calls, then text) -> OK."""

    def __init__(self):
        self.calls = 0

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
        from app.providers import Delta, ToolCall, Usage

        self.calls += 1
        if self.calls == 1:  # spec writer
            yield Delta(text=MINIMAL_SPEC)
        elif self.calls == 2:  # spec ai-review
            yield Delta(text="Spec looks good.\n\nVERDICT: APPROVED")
        elif self.calls in (3, 4):  # coder rounds 1 and 2: tool each
            yield ToolCall(
                id=f"t{self.calls}",
                name="write_file",
                arguments=json.dumps(
                    {"path": f"f{self.calls}.py", "content": f"V{self.calls} = 1\n"}
                ),
            )
        elif self.calls == 5:  # coder final answer
            yield Delta(text="Done.")
        else:  # code review
            yield Delta(text="diff matches spec\n\nVERDICT: APPROVED")
        yield Usage(prompt_tokens=1, completion_tokens=1)


# ---- (a) PATCH /settings tools_max_calls=120 -> 200, GET shows 120 ----------


def test_tools_max_calls_accepts_120(client):
    res = client.patch("/api/v1/settings", json={"tools_max_calls": 120})
    assert res.status_code == 200, res.text
    assert res.json()["tools_max_calls"] == 120
    assert client.get("/api/v1/settings").json()["tools_max_calls"] == 120


def test_tools_max_calls_rejects_161_and_zero(client):
    assert client.patch("/api/v1/settings", json={"tools_max_calls": 161}).status_code == 422
    # 0 must be rejected, not treated as "unlimited"
    assert client.patch("/api/v1/settings", json={"tools_max_calls": 0}).status_code == 422
    assert client.get("/api/v1/settings").json()["tools_max_calls"] == 40


def test_story_budget_rejects_161_and_zero(client):
    assert (
        client.post(
            "/api/v1/stories", json={"title": "x", "max_tool_calls": 161}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/stories", json={"title": "x", "max_tool_calls": 0}
        ).status_code
        == 422
    )
    story = client.post("/api/v1/stories", json={"title": "x"}).json()
    assert client.patch(
        f"/api/v1/stories/{story['id']}", json={"max_tool_calls": 161}
    ).status_code == 422
    assert client.get(f"/api/v1/stories/{story['id']}").json()["max_tool_calls"] is None


# ---- (b) story with max_tool_calls=120 runs; coder rounds report 120 --------


def _run_story(client, budget: dict | None):
    payload = {"title": "budget story"}
    if budget is not None:
        payload["max_tool_calls"] = budget
    story = client.post("/api/v1/stories", json=payload).json()
    if budget is not None:
        assert story["max_tool_calls"] == budget
    client.post(f"/api/v1/stories/{story['id']}/queue")
    wait_for_status(client, story["id"], ("human_review", "failed"))
    client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
    final = wait_for_status(client, story["id"], ("human_review", "failed"))
    assert final["status"] == "human_review", final.get("error")
    detail = client.get(f"/api/v1/stories/{story['id']}").json()
    rounds = [d for k, d in activity_from(detail) if k == "round"]
    return story, rounds


def test_story_override_budget_round_events(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "budget.db"))
    with TestClient(create_app(gateway=TwoToolCoderGateway())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "tools_max_calls": 8,
            },
        )
        story, rounds = _run_story(client, 120)
        coder_rounds = [r for r in rounds if r.get("phase") == "coder"]
        assert coder_rounds, rounds
        # the story override wins over the app setting (8)
        assert all(r["max_tool_calls"] == 120 for r in coder_rounds), coder_rounds
        # spec rounds are unchanged: they keep the app default
        spec_rounds = [r for r in rounds if r.get("phase") == "spec"]
        assert spec_rounds and all(r["max_tool_calls"] == 8 for r in spec_rounds)


def test_story_without_override_uses_app_setting(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "fallback.db"))
    with TestClient(create_app(gateway=TwoToolCoderGateway())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "tools_max_calls": 8,
            },
        )
        story, rounds = _run_story(client, None)
        assert story["max_tool_calls"] is None
        coder_rounds = [r for r in rounds if r.get("phase") == "coder"]
        assert coder_rounds, rounds
        assert all(r["max_tool_calls"] == 8 for r in coder_rounds), coder_rounds


# ---- spec-gate PATCH / approve payload --------------------------------------


def test_spec_gate_patch_budget_runs_coder_with_it(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "gate.db"))
    with TestClient(create_app(gateway=TwoToolCoderGateway())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "tools_max_calls": 8,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "gate story"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        wait_for_status(client, story["id"], ("human_review", "failed"))

        # the operator raises the budget at the spec gate (approve payload,
        # same route as the model switch — human_review PATCH is 409 by design)
        res = client.post(
            f"/api/v1/stories/{story['id']}/approve",
            json={"by": "t", "max_tool_calls": 120},
        )
        assert res.status_code == 200, res.text
        assert res.json()["max_tool_calls"] == 120

        wait_for_status(client, story["id"], ("human_review", "failed"))
        detail = client.get(f"/api/v1/stories/{story['id']}").json()
        coder_rounds = [
            d
            for k, d in activity_from(detail)
            if k == "round" and d.get("phase") == "coder"
        ]
        assert coder_rounds
        assert all(r["max_tool_calls"] == 120 for r in coder_rounds), coder_rounds
