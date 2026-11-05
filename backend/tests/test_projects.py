"""Projects and the permission guardrail with live operator decisions."""

import asyncio
import json

import pytest

from app.agent import run_agent
from app.agent_tools import ToolBox
from app.guardrails import GuardedExecutor, ToolPolicy
from app.providers import Delta, ToolCall, Usage


@pytest.fixture
def two_dirs(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "inside.txt").write_text("inside content")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "notes.txt").write_text("outside content")
    return project, outside


def test_projects_crud(client):
    res = client.post(
        "/api/v1/projects", json={"name": "Fubala", "root": "/tmp", "extra_paths": []}
    )
    assert res.status_code == 201
    project = res.json()
    assert project["name"] == "Fubala" and project["root_exists"]

    listed = client.get("/api/v1/projects").json()
    assert any(p["id"] == project["id"] for p in listed)

    patched = client.patch(
        f"/api/v1/projects/{project['id']}", json={"name": "Fubala 2"}
    ).json()
    assert patched["name"] == "Fubala 2"

    story = client.post(
        "/api/v1/stories",
        json={"title": "in project", "project_id": project["id"]},
    ).json()
    assert story["project_id"] == project["id"]
    assert client.get("/api/v1/projects").json()[0]["story_count"] == 1

    assert client.delete(f"/api/v1/projects/{project['id']}").status_code == 204
    orphan = client.get(f"/api/v1/stories/{story['id']}").json()
    assert orphan["project_id"] is None


def test_outside_path_blocked_until_granted(two_dirs):
    project, outside = two_dirs
    box = ToolBox(engine=None, project_root=str(project))
    result = box.execute("read_file", json.dumps({"path": str(outside / "notes.txt")}))
    assert "escapes the project roots" in result
    assert "request_permission" in result


def _perm_gateway():
    class Gateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            self.calls += 1
            if self.calls == 1:
                yield ToolCall(
                    id="p1",
                    name="request_permission",
                    arguments=json.dumps(
                        {"path": "/abs/elsewhere", "reason": "need shared config"}
                    ),
                )
                yield Usage(prompt_tokens=1, completion_tokens=1)
            else:
                assert any(
                    t["function"]["name"] == "read_file" for t in tools or []
                )
                yield Delta(text="permission was ")
                yield Delta(text="granted, proceeding")
                yield Usage(prompt_tokens=2, completion_tokens=3)

    return Gateway()


def test_permission_live_approve(two_dirs, client):
    """The model asks; the operator approves live; the run continues."""
    project, outside = two_dirs
    # make the requested path the real outside dir
    box = ToolBox(engine=None, project_root=str(project))
    policy = ToolPolicy(permission_mode="ask")
    guarded = GuardedExecutor(
        box, policy, engine=client.app.state.engine, context={"session_id": "s1"}
    )

    # point the scripted request at the real outside dir
    class Gateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            self.calls += 1
            if self.calls == 1:
                assert any(t["function"]["name"] == "request_permission" for t in tools or [])
                yield ToolCall(
                    id="p1",
                    name="request_permission",
                    arguments=json.dumps(
                        {"path": str(outside), "reason": "need shared config"}
                    ),
                )
                yield Usage(prompt_tokens=1, completion_tokens=1)
            else:
                # after approval the outside file must be readable
                fs = box.execute("read_file", json.dumps({"path": str(outside / "notes.txt")}))
                assert "outside content" in fs
                yield Delta(text="approved and read the file")
                yield Usage(prompt_tokens=2, completion_tokens=2)

    async def approve_when_pending():
        from sqlmodel import Session, select

        from app.models import PermissionRequest

        for _ in range(100):
            await asyncio.sleep(0.1)
            with Session(client.app.state.engine) as db:
                row = db.exec(
                    select(PermissionRequest).where(
                        PermissionRequest.status == "pending"  # type: ignore[arg-type]
                    )
                ).first()
                request_id = row.id if row else None
            if request_id is None:
                continue
            res = client.post(
                f"/api/v1/permissions/{request_id}/decide",
                json={"decision": "approve", "remember": False},
            )
            assert res.json()["status"] == "approved"
            return
        raise AssertionError("no pending permission request appeared")

    async def go():
        return await run_agent(
            Gateway(),
            model="echo/dev",
            messages=[{"role": "user", "content": "go"}],
            tools=box.schemas(),
            execute=guarded,
        )

    async def scenario():
        done = await asyncio.wait_for(
            asyncio.gather(go(), approve_when_pending()), timeout=60
        )
        return done[0]

    result = asyncio.run(scenario())
    assert result.text == "approved and read the file"
    assert result.tool_calls == 1


def test_permission_skip_mode_auto_grants(two_dirs):
    project, outside = two_dirs
    box = ToolBox(engine=None, project_root=str(project))
    guarded = GuardedExecutor(box, ToolPolicy(permission_mode="skip"))

    async def go():
        return await guarded(
            "request_permission",
            json.dumps({"path": str(outside), "reason": "r"}),
        )

    result = asyncio.run(go())
    assert "auto-approved" in result
    # and the root is now usable in this run
    assert "outside content" in box.execute(
        "read_file", json.dumps({"path": str(outside / "notes.txt")})
    )


def test_permission_strict_mode_denies(two_dirs):
    box = ToolBox(engine=None, project_root=str(two_dirs[0]))
    guarded = GuardedExecutor(box, ToolPolicy(permission_mode="strict"))

    async def go():
        return await guarded(
            "request_permission", json.dumps({"path": "/abs/x", "reason": "r"})
        )

    result = asyncio.run(go())
    assert "strict mode" in result


def test_fs_browse_lists_directories(tmp_path, monkeypatch, client):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "Beta").mkdir()
    (tmp_path / "file.txt").write_text("x")
    res = client.get("/api/v1/projects/fs/browse", params={"path": str(tmp_path)})
    assert res.status_code == 200
    body = res.json()
    assert body["path"] == str(tmp_path)
    assert body["parent"] is not None
    names = [d["name"] for d in body["dirs"]]
    assert names == ["alpha", "Beta"]  # sorted, files excluded

    bad = client.get("/api/v1/projects/fs/browse", params={"path": str(tmp_path / "file.txt")})
    assert bad.status_code == 400

    home = client.get("/api/v1/projects/fs/browse")
    assert home.status_code == 200 and home.json()["dirs"] is not None


# ---- project executor (v0.10) -------------------------------------------


def test_project_executor_defaults_to_internal(client):
    """Created without executor, a project inherits the internal runner."""
    res = client.post(
        "/api/v1/projects", json={"name": "Default", "root": "/tmp"}
    )
    assert res.status_code == 201
    assert res.json()["executor"] == "internal"


def test_project_executor_set_on_create(client):
    res = client.post(
        "/api/v1/projects",
        json={"name": "Handoff", "root": "/tmp", "executor": "external"},
    )
    assert res.status_code == 201
    assert res.json()["executor"] == "external"


def test_project_executor_invalid_on_create_rejected(client):
    res = client.post(
        "/api/v1/projects",
        json={"name": "Bad", "root": "/tmp", "executor": "sidecar"},
    )
    assert res.status_code == 422
    assert "executor" in res.json()["detail"][0]["msg"]


def test_project_executor_toggle_and_validation(client):
    """PATCH can flip the executor; anything but internal/external is a 400."""
    project = client.post(
        "/api/v1/projects", json={"name": "Flip", "root": "/tmp"}
    ).json()

    toggled = client.patch(
        f"/api/v1/projects/{project['id']}", json={"executor": "external"}
    )
    assert toggled.status_code == 200
    assert toggled.json()["executor"] == "external"

    # and back
    back = client.patch(
        f"/api/v1/projects/{project['id']}", json={"executor": "internal"}
    )
    assert back.status_code == 200
    assert back.json()["executor"] == "internal"

    bad = client.patch(
        f"/api/v1/projects/{project['id']}", json={"executor": "hybrid"}
    )
    assert bad.status_code == 400
    assert "executor" in bad.json()["detail"]

    # a failed patch must not have mutated the stored value
    assert (
        client.get("/api/v1/projects").json()[0]["executor"] == "internal"
    )


def test_project_executor_toggle_never_rewrites_stories(client):
    """Inheritance is read-time: existing story rows keep their NULL
    (inherit) or concrete (override) values when the project flips."""
    project = client.post(
        "/api/v1/projects", json={"name": "Stable", "root": "/tmp"}
    ).json()
    inheriting = client.post(
        "/api/v1/stories",
        json={"title": "inherit story", "project_id": project["id"]},
    ).json()
    override = client.post(
        "/api/v1/stories",
        json={
            "title": "override story",
            "project_id": project["id"],
            "execution": "internal",
        },
    ).json()

    assert (
        client.patch(
            f"/api/v1/projects/{project['id']}", json={"executor": "external"}
        ).status_code
        == 200
    )

    stories = {s["id"]: s for s in client.get("/api/v1/stories").json()}
    assert stories[inheriting["id"]]["execution_override"] is None
    assert stories[inheriting["id"]]["execution"] == "external"
    assert stories[override["id"]]["execution_override"] == "internal"
    assert stories[override["id"]]["execution"] == "internal"
