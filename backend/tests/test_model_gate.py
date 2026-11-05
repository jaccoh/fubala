"""Spec-gate model switching (story: switch coder model at the spec gate).

At the spec gate (human_review, no executed work) the operator can change
the coder model — via PATCH {model} or via `model` on the approve call. The
code gate (base_ref set) stays fully locked, external stories refuse model
changes, and MCP's story_action only forwards a non-empty model.
"""

import asyncio
import json
import time

from app import mcp_tools as mcp


def _wait_for_status(client, story_id: str, want: tuple[str, ...], timeout: float = 30.0):
    deadline = time.time() + timeout
    current = client.get(f"/api/v1/stories/{story_id}").json()
    while time.time() < deadline:
        current = client.get(f"/api/v1/stories/{story_id}").json()
        if current["status"] in want:
            return current
        time.sleep(0.05)
    return current


def _events(story: dict, kind: str) -> list[dict]:
    return [json.loads(e["data"]) for e in story["events"] if e["kind"] == kind]


# ---- full-cycle gateway -----------------------------------------------------


class ScriptedGateway:
    """spec -> ai review approve -> coder (write + text) -> code review OK.

    Records the model of every call so tests can assert the coder ran on the
    switched model."""

    def __init__(self):
        self.calls = 0
        self.models: list[str] = []

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
        from app.providers import Delta, ToolCall, Usage

        self.calls += 1
        self.models.append(model)
        last = messages[-1].get("content") if messages else ""
        if isinstance(last, str) and last.startswith("=== SPEC REQUEST ==="):
            yield Delta(
                text=(
                    "## Goal\nAdd the thing.\n\n"
                    "## Implementation steps\n1. Do it\n\n"
                    "## Acceptance checks\n- [ ] tests pass\n"
                )
            )
        elif self.calls == 2:  # ai review of the spec
            yield Delta(text="fine\n\nVERDICT: APPROVED")
        elif self.calls == 3:  # coder: one write, then done
            yield ToolCall(
                id="w1",
                name="write_file",
                arguments=json.dumps({"path": "feature.py", "content": "X = 1\n"}),
            )
        elif self.calls == 4:  # coder final text
            yield Delta(text="wrote feature.py")
        else:  # code review
            yield Delta(text="diff matches spec\n\nVERDICT: APPROVED")
        yield Usage(prompt_tokens=1, completion_tokens=1)


def _app(client):
    return client


# ---- (a) spec-gate PATCH {model}: 200 + coder runs on the new model ---------


def test_spec_gate_patch_model_runs_coder_on_it(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_execution import init_repo

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "a.db"))
    gateway = ScriptedGateway()
    with TestClient(create_app_local(gateway=gateway)) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": str(repo),
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "feature X"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        current = _wait_for_status(client, story["id"], ("human_review", "failed"))
        assert current["status"] == "human_review", current.get("error")

        # the operator switches the coder model at the spec gate
        res = client.patch(
            f"/api/v1/stories/{story['id']}", json={"model": "echo/dev"}
        )
        assert res.status_code == 200, res.text
        assert res.json()["model"] == "echo/dev"
        # the routing-note convention: old -> new model on a note event
        # (events live on the detail endpoint, not on StoryOut)
        detail = client.get(f"/api/v1/stories/{story['id']}").json()
        notes = _events(detail, "note")
        assert any(
            n.get("model_switch") and n.get("to") == "echo/dev" for n in notes
        ), notes

        # approving runs the coder on the switched model (call 3 = coder)
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        _wait_for_status(client, story["id"], ("human_review", "failed", "done"))
        assert gateway.models[2] == "echo/dev", gateway.models


# ---- (b) code-gate PATCH {model}: 409 ----------------------------------------


def test_code_gate_patch_model_is_409(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_execution import init_repo

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "b.db"))
    gateway = ScriptedGateway()
    with TestClient(create_app_local(gateway=gateway)) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": str(repo),
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "feature X"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        current = _wait_for_status(client, story["id"], ("human_review", "failed"))
        assert current["status"] == "human_review", current.get("error")

        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        current = _wait_for_status(client, story["id"], ("human_review", "failed"))
        assert current["status"] == "human_review" and current["base_ref"], current

        # the code gate is locked: a model PATCH 409s and changes nothing
        res = client.patch(f"/api/v1/stories/{story['id']}", json={"model": "echo/dev"})
        assert res.status_code == 409
        assert client.get(f"/api/v1/stories/{story['id']}").json()["model"] == "echo/dev"


# ---- (c) spec-gate approve with model: set before EXECUTING ------------------


def test_spec_gate_approve_with_model_runs_coder_on_it(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_execution import init_repo

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "c.db"))
    gateway = ScriptedGateway()
    with TestClient(create_app_local(gateway=gateway)) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": str(repo),
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "feature X"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        current = _wait_for_status(client, story["id"], ("human_review", "failed"))
        assert current["status"] == "human_review", current.get("error")

        approved = client.post(
            f"/api/v1/stories/{story['id']}/approve",
            json={"by": "t", "model": "echo/dev"},
        )
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert body["status"] == "executing"
        assert body["model"] == "echo/dev", "model must be set before EXECUTING"
        # events live on the detail endpoint, not on StoryOut
        detail = client.get(f"/api/v1/stories/{story['id']}").json()
        notes = _events(detail, "note")
        assert any(
            n.get("model_switch") and n.get("to") == "echo/dev" for n in notes
        ), notes

        _wait_for_status(client, story["id"], ("human_review", "failed", "done"))
        assert gateway.models[2] == "echo/dev", gateway.models


# ---- (d) code-gate approve with model: 409 -----------------------------------


def test_code_gate_approve_with_model_is_409(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_execution import init_repo

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "d.db"))
    gateway = ScriptedGateway()
    with TestClient(create_app_local(gateway=gateway)) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": str(repo),
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "feature X"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        current = _wait_for_status(client, story["id"], ("human_review", "failed"))
        assert current["status"] == "human_review", current.get("error")

        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        current = _wait_for_status(client, story["id"], ("human_review", "failed"))
        assert current["status"] == "human_review" and current["base_ref"], current

        # the model must be rejected, not silently applied
        res = client.post(
            f"/api/v1/stories/{story['id']}/approve",
            json={"by": "t", "model": "echo/dev"},
        )
        assert res.status_code == 409

        # the story still approves without the model key
        ok = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        assert ok.status_code == 200
        assert ok.json()["status"] == "done"


# ---- external stories at the gate refuse model changes -----------------------


def test_external_story_at_gate_refuses_model_changes(client):
    """An external story in human_review has no coder run ahead of it — both
    the PATCH and the approve must refuse a model (409) instead of silently
    setting a model nothing will ever use."""
    story_res = client.post("/api/v1/stories", json={"title": "ext", "execution": "external"})
    assert story_res.status_code == 201
    story = story_res.json()
    assert client.post(f"/api/v1/stories/{story['id']}/queue").status_code == 200
    assert (
        client.post(f"/api/v1/stories/{story['id']}/claim", json={"claimed_by": "bot"})
        .status_code
        == 200
    )
    done = client.post(
        f"/api/v1/stories/{story['id']}/complete", json={"note": "landed outside"}
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "human_review"

    # creation stamps the default model (echo/dev here); neither attempt may
    # change it — and no model_switch note may appear
    before = client.get(f"/api/v1/stories/{story['id']}").json()
    assert before["model"], before

    patch = client.patch(f"/api/v1/stories/{story['id']}", json={"model": "zai/glm-4.7"})
    assert patch.status_code == 409
    approve = client.post(
        f"/api/v1/stories/{story['id']}/approve",
        json={"by": "t", "model": "zai/glm-4.7"},
    )
    assert approve.status_code == 409

    after = client.get(f"/api/v1/stories/{story['id']}").json()
    assert after["model"] == before["model"]
    assert not any(
        n.get("model_switch") for n in _events(after, "note")
    ), _events(after, "note")
    # and the story still approves cleanly without a model key
    ok = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "done"



# ---- (f) other fields stay locked; unknown model 400s ------------------------


def test_spec_gate_patch_other_fields_stay_locked(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_execution import init_repo

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "f.db"))
    gateway = ScriptedGateway()
    with TestClient(create_app_local(gateway=gateway)) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": str(repo),
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        story = client.post(
            "/api/v1/stories", json={"title": "feature X", "skill": "low"}
        ).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        current = _wait_for_status(client, story["id"], ("human_review", "failed"))
        assert current["status"] == "human_review", current.get("error")
        # skill=low routed to the echo model at pickup
        assert current["model"] == "echo/dev"

        # any other field at the spec gate stays a 409 (editable-status rule)
        assert client.patch(
            f"/api/v1/stories/{story['id']}", json={"title": "nope"}
        ).status_code == 409
        assert client.patch(
            f"/api/v1/stories/{story['id']}", json={"description": "nope"}
        ).status_code == 409
        # combined {model, title} is not a bare model switch either
        assert client.patch(
            f"/api/v1/stories/{story['id']}", json={"model": "zai/glm-4.7", "title": "x"}
        ).status_code == 409

        # unknown model -> 400
        assert client.patch(
            f"/api/v1/stories/{story['id']}", json={"model": "nope/none"}
        ).status_code == 400
        # explicit null is NOT a bare switch: it stays 409 (no silent
        # fallback to skill routing)
        assert client.patch(
            f"/api/v1/stories/{story['id']}", json={"model": None}
        ).status_code == 409

        # the story still switches cleanly afterwards
        res = client.patch(f"/api/v1/stories/{story['id']}", json={"model": "zai/glm-4.7"})
        assert res.status_code == 200
        assert res.json()["model"] == "zai/glm-4.7"


# ---- (e) MCP story_action approve: model forwarding --------------------------
# (direct-call mechanics copied from test_mcp_gate.py: monkeypatch mcp._post)


def _setup_post(monkeypatch, story: dict | None = None):
    captured = {}

    async def scripted_post(path, body):
        captured["path"] = path
        captured["body"] = body
        return story or {
            "id": "story-1",
            "title": "Gate me",
            "status": "human_review",
            "depends_on": [],
            "spec_version": 1,
            "model": "zai/glm-5.3",
            "skill": None,
            "execution": "internal",
            "execution_override": None,
            "claimed_by": None,
            "ai_verdict": "approve",
            "error": None,
            "description": "",
            "acceptance": "",
            "spec": None,
            "verify_result": None,
            "ai_review": None,
            "evidence": None,
            "events": [],
            "notes": [],
        }

    monkeypatch.setattr(mcp, "_post", scripted_post)
    return captured


def test_mcp_approve_without_model_sends_no_model_key(monkeypatch):
    captured = _setup_post(monkeypatch)
    out = asyncio.run(mcp.story_action("story-1", "approve"))
    assert "action failed" not in out
    assert captured["path"] == "/stories/story-1/approve"
    assert captured["body"] == {}


def test_mcp_approve_blank_model_sends_no_model_key(monkeypatch):
    """The rejected attempt's bug: an unconditional `model: ""` merge would
    collide with ApproveBody's `is not None` check and 400 every MCP approve."""
    captured = _setup_post(monkeypatch)
    out = asyncio.run(mcp.story_action("story-1", "approve", model="   "))
    assert "action failed" not in out
    assert captured["body"] == {}


def test_mcp_approve_forwards_model(monkeypatch):
    captured = _setup_post(monkeypatch)
    out = asyncio.run(mcp.story_action("story-1", "approve", model="echo/dev"))
    assert "action failed" not in out
    assert captured["body"] == {"model": "echo/dev"}


def test_mcp_approve_model_merges_with_override_verify(monkeypatch):
    captured = _setup_post(monkeypatch)
    out = asyncio.run(
        mcp.story_action(
            "story-1",
            "approve",
            override_verify=True,
            justification="accepted",
            model="echo/dev",
        )
    )
    assert "action failed" not in out
    assert captured["body"] == {
        "override_verify": True,
        "justification": "accepted",
        "model": "echo/dev",
    }


def test_mcp_approve_model_not_merged_for_other_actions(monkeypatch):
    """model is approve-only: reject/cancel bodies must stay untouched."""
    captured = _setup_post(monkeypatch)
    out = asyncio.run(
        mcp.story_action("story-1", "reject", reason="gripe", model="echo/dev")
    )
    assert "action failed" not in out
    assert captured["body"] == {"reason": "gripe"}


def test_mcp_approve_with_model_end_to_end(tmp_path, monkeypatch):
    """Full path: MCP tool -> HTTP API -> model set at the spec gate before
    EXECUTING; and without model the approve behaves exactly as before."""
    from fastapi.testclient import TestClient
    from test_execution import init_repo

    import app.mcp_tools as mcp_mod

    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "e2e.db"))
    gateway = ScriptedGateway()
    with TestClient(create_app_local(gateway=gateway)) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": str(repo),
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "feature X"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        current = _wait_for_status(client, story["id"], ("human_review", "failed"))
        assert current["status"] == "human_review", current.get("error")

        mcp_mod.bind_app(client.app)
        out = asyncio.run(
            mcp_mod.story_action(story["id"], "approve", model="echo/dev")
        )
        assert "action failed" not in out, out

        body = client.get(f"/api/v1/stories/{story['id']}").json()
        assert body["model"] == "echo/dev"
        notes = _events(body, "note")
        assert any(n.get("model_switch") for n in notes), notes

        _wait_for_status(client, story["id"], ("human_review", "failed", "done"))
        assert gateway.models[2] == "echo/dev", gateway.models


# ---- shared app factory (backend/tests imports are package-relative) ---------


def create_app_local(gateway):
    from app.main import create_app

    return create_app(gateway=gateway)


# ---- inherited external: story NULL + project executor=external --------------


def _external_project(client) -> dict:
    proj = client.post(
        "/api/v1/projects",
        json={"name": "ext-proj", "root": "/tmp/ext-proj", "executor": "external"},
    ).json()
    assert proj.get("id"), proj
    return proj


def test_inherited_external_story_refuses_model_changes(client):
    """The stored-execution test above covers story.execution == external.
    This covers the inheritance path: the story's execution is NULL and the
    owning project's executor is external — effective_execution() resolves to
    external, so both the PATCH and the approve must 409 even though a
    bare-column check would pass."""
    story_res = client.post(
        "/api/v1/stories",
        json={"title": "inh ext", "project_id": _external_project(client)["id"]},
    )
    assert story_res.status_code == 201, story_res.text
    story = story_res.json()

    # at the spec gate the PATCH must refuse: the effective execution is
    # external (inherited), so no coder run can ever follow
    patch = client.patch(f"/api/v1/stories/{story['id']}", json={"model": "echo/dev"})
    assert patch.status_code == 409, patch.text
    approve = client.post(
        f"/api/v1/stories/{story['id']}/approve", json={"by": "t", "model": "echo/dev"}
    )
    assert approve.status_code == 409
    # ...and the approve with a model did not consume the gate either
    current = client.get(f"/api/v1/stories/{story['id']}").json()
    assert current["status"] == "backlog", current

    # drive it through the real external flow to the human gate: claim
    # materializes execution=external, then complete -> human_review
    assert client.post(f"/api/v1/stories/{story['id']}/queue").status_code == 200
    claimed = client.post(
        f"/api/v1/stories/{story['id']}/claim", json={"claimed_by": "bot"}
    )
    assert claimed.status_code == 200, claimed.text
    done = client.post(
        f"/api/v1/stories/{story['id']}/complete", json={"note": "landed outside"}
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "human_review"

    # still locked at the gate, on both endpoints, by the effective value
    patch = client.patch(f"/api/v1/stories/{story['id']}", json={"model": "echo/dev"})
    assert patch.status_code == 409, patch.text
    approve = client.post(
        f"/api/v1/stories/{story['id']}/approve", json={"by": "t", "model": "echo/dev"}
    )
    assert approve.status_code == 409, approve.text
    after = client.get(f"/api/v1/stories/{story['id']}").json()
    assert after["model"] != "echo/dev"
    assert not any(
        n.get("model_switch") for n in _events(after, "note")
    ), _events(after, "note")
    # and it still approves cleanly without a model key
    ok = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "done"
