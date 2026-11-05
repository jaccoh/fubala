"""Story kinds: code (full pipeline) | info | prep (approval finishes).

info/prep run the spec -> AI review pipeline but never create a worktree;
approval completes them (prep only once a child story links back via
depends_on). Code behavior is byte-for-byte unchanged.
"""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import workspaces

from conftest import MINIMAL_SPEC
from app.main import create_app


def create_story(client, **body) -> dict:
    res = client.post("/api/v1/stories", json={"title": "Doc the API", **body})
    assert res.status_code == 201, res.text
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


# ---- validation & defaults ----------------------------------------------


def test_kind_must_be_known_on_create(client):
    res = client.post("/api/v1/stories", json={"title": "x", "kind": "essay"})
    assert res.status_code == 400
    assert "kind must be one of" in res.json()["detail"]


def test_kind_must_be_known_on_update(client):
    story = create_story(client)
    res = client.patch(f"/api/v1/stories/{story['id']}", json={"kind": "poem"})
    assert res.status_code == 400
    assert "kind must be one of" in res.json()["detail"]


@pytest.mark.parametrize("kind", ["code", "info", "prep"])
def test_each_kind_persists(client, kind):
    assert create_story(client, kind=kind)["kind"] == kind


def test_kind_defaults_to_code(client):
    assert create_story(client)["kind"] == "code"


def test_legacy_db_migrates_kind_and_survives_the_rebuild(tmp_path, monkeypatch):
    """A pre-v0.9 database (no `kind`, model still NOT NULL) must gain the
    column defaulting to code — and the `_rebuild_stories` path that the
    NOT NULL model triggers must carry `kind` across, not drop it."""
    from sqlalchemy import text

    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE stories (
            id VARCHAR NOT NULL PRIMARY KEY,
            title VARCHAR(200) NOT NULL,
            description TEXT NOT NULL,
            acceptance TEXT NOT NULL,
            status VARCHAR NOT NULL,
            model VARCHAR NOT NULL,
            created_by VARCHAR NOT NULL DEFAULT 'local',
            spec TEXT,
            spec_version INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        INSERT INTO stories (id, title, description, acceptance, status, model,
                             spec_version, created_at, updated_at)
        VALUES ('legacy1', 'Old story', '', '', 'backlog', 'm', 0,
                '2024-01-01', '2024-01-01');
        """
    )
    conn.commit()
    conn.close()

    with TestClient(create_app(gateway=None)) as client:
        legacy = client.get("/api/v1/stories/legacy1").json()
        assert legacy["kind"] == "code"  # NULL-safe default for legacy rows

        fresh = create_story(client)  # new rows are code too
        assert fresh["kind"] == "code"

        with client.app.state.engine.begin() as sql:
            rows = sql.exec_driver_sql("PRAGMA table_info(stories)").fetchall()
        cols = {r[1] for r in rows}
        # the rebuild (triggered by the NOT NULL model column) must keep kind
        assert "kind" in cols
        with client.app.state.engine.begin() as sql:
            kinds = sql.execute(text("SELECT kind FROM stories")).fetchall()
        assert all(k[0] == "code" for k in kinds)


# ---- info: document deliverable, approval finishes ----------------------


def test_info_story_never_creates_a_worktree(client, monkeypatch):
    created: list[str] = []
    real_create = workspaces.create

    def spy(root, story_id):
        created.append(story_id)
        return real_create(root, story_id)

    monkeypatch.setattr(workspaces, "create", spy)

    story = create_story(client, kind="info")
    client.post(f"/api/v1/stories/{story['id']}/queue")

    done = wait_for_status(client, story["id"], "human_review")
    assert done["base_ref"] is None
    assert done["spec_version"] == 1 and done["spec"]
    assert done["verify_result"] == {"applicable": False}

    final = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"}).json()
    assert final["status"] == "done"
    assert final["verify_result"] == {"applicable": False}
    assert created == [], "info story must never open a worktree"


def test_info_and_prep_prompts_tell_the_agent_the_deliverable(client, gateway):
    story = create_story(client, kind="info")
    client.post(f"/api/v1/stories/{story['id']}/queue")
    wait_for_status(client, story["id"], "human_review")

    spec_prompt = gateway.calls[0]["messages"][1]["content"]
    review_prompt = gateway.calls[1]["messages"][1]["content"]
    assert "Story kind: info" in spec_prompt
    assert "no code will be executed" in spec_prompt
    # the reviewer judges the same artifact, not "where is the code?"
    assert "Story kind: info" in review_prompt


def test_rejected_info_story_reruns_spec_pipeline(client):
    """Reject requeues with no base_ref, so the spec pipeline picks it up
    again; the n/a verify stamp is idempotent."""
    story = create_story(client, kind="info")
    client.post(f"/api/v1/stories/{story['id']}/queue")
    first = wait_for_status(client, story["id"], "human_review")

    client.post(f"/api/v1/stories/{story['id']}/reject", json={"reason": "add usage"})

    second = wait_for_status(client, story["id"], "human_review")
    assert second["spec_version"] == first["spec_version"] + 1
    assert second["base_ref"] is None
    assert second["verify_result"] == {"applicable": False}

    assert client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"}).json()["status"] == "done"


# ---- prep: child stories are the deliverable ----------------------------


def test_prep_approve_requires_a_linked_child(client):
    story = create_story(client, kind="prep", acceptance="children planned")
    client.post(f"/api/v1/stories/{story['id']}/queue")
    wait_for_status(client, story["id"], "human_review")
    assert client.get(f"/api/v1/stories/{story['id']}").json()["verify_result"] == {
        "applicable": False
    }

    blocked = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
    assert blocked.status_code == 409
    assert "prep story has no linked children" in blocked.json()["detail"]

    child = create_story(client, title="Child step", depends_on=[story["id"]])
    assert child["depends_on"] == [story["id"]]

    ok = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "done"


def test_prep_child_must_actually_depend_on_the_story(client):
    """The child check parses depends_on in Python — a row whose stored JSON
    merely contains the parent id as a *substring* (a SQL LIKE would match
    it) must not unblock the parent."""
    from sqlalchemy import text

    story = create_story(client, kind="prep")
    client.post(f"/api/v1/stories/{story['id']}/queue")
    wait_for_status(client, story["id"], "human_review")

    # sneak in a row whose depends_on contains the id as a substring only
    decoy_id = story["id"] + "decoy"
    with client.app.state.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO stories (id, title, description, acceptance, status,"
                " created_by, execution, kind, depends_on, spec_version,"
                " created_at, updated_at)"
                " VALUES (:i, 'Decoy', '', '', 'backlog', 'local', 'internal',"
                " 'code', :d, 0, :t, :t)"
            ),
            {"i": decoy_id, "d": json.dumps([story["id"] + "tail"]), "t": "2024-01-01"},
        )

    blocked = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
    assert blocked.status_code == 409
    assert "prep story has no linked children" in blocked.json()["detail"]

    # a real child (validated by the API) unblocks it
    create_story(client, title="Real child", depends_on=[story["id"]])
    ok = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "done"


# ---- code regression: the full worktree pipeline ------------------------


def init_repo(tmp_path) -> str:
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n")
    for args in (["git", "init", "-q"], ["git", "add", "-A"]):
        subprocess_run_ok(repo, *args)
    subprocess_run_ok(
        repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"
    )
    return str(repo)


def subprocess_run_ok(cwd, *args):
    import subprocess

    res = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return res


def test_code_story_still_walks_the_full_pipeline(tmp_path, monkeypatch):
    """kind omitted: spec -> approve -> coder worktree -> merge, exactly as
    before the kind column existed."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "kinds-code.db"))

    class ScriptedGateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            from app.providers import Delta as D
            from app.providers import ToolCall as TC
            from app.providers import Usage as U

            self.calls += 1
            if self.calls == 1:  # spec
                yield D(text=MINIMAL_SPEC)
            if self.calls == 2:  # ai review of spec
                yield D(text="fine\n\nVERDICT: APPROVED")
            if self.calls == 3:  # coder: write a file
                yield TC(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps(
                        {"path": "feature.py", "content": "VALUE = 1\n"}
                    ),
                )
            if self.calls == 4:  # coder final text
                yield D(text="implemented feature.py")
            if self.calls == 5:  # code review
                yield D(text="diff matches spec\n\nVERDICT: APPROVED")
            yield U(prompt_tokens=1, completion_tokens=1)

    with TestClient(create_app(gateway=ScriptedGateway())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "feature X"}).json()
        assert story["kind"] == "code"
        client.post(f"/api/v1/stories/{story['id']}/queue")

        def settle(timeout):
            deadline = time.time() + timeout
            current = {}
            while time.time() < deadline:
                current = client.get(f"/api/v1/stories/{story['id']}").json()
                if current["status"] in ("human_review", "failed"):
                    break
                time.sleep(0.05)
            assert current["status"] == "human_review", current.get("error")
            return current

        settle(30)
        # the spec-stage review carries no n/a stamp — that is info/prep only
        assert settle(30)["verify_result"] != {"applicable": False}

        first = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"}).json()
        assert first["status"] == "executing"

        reviewed = settle(30)
        assert reviewed["base_ref"], "code story must have a worktree base"
        assert "feature.py" in (reviewed.get("code_diff") or "")

        diff = client.get(f"/api/v1/stories/{story['id']}/diff")
        assert diff.status_code == 200, diff.text
        assert "feature.py" in diff.text

        final = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"}).json()
        assert final["status"] == "done"
        assert (Path(repo) / "feature.py").exists()
        assert not (Path(repo) / ".fubala-worktrees" / story["id"]).exists()


def test_prep_spec_writer_creates_children_and_approval_passes(tmp_path, monkeypatch):
    from conftest import FakeGateway

    from app.providers import ToolCall

    class PrepGateway(FakeGateway):
        prep_id = ""
        schemas: list = []

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            last = messages[-1]
            first_msg = messages[1].get("content") if len(messages) > 1 else ""
            if (
                isinstance(first_msg, str)
                and first_msg.startswith("=== SPEC REQUEST ===")
                and last.get("role") != "tool"
            ):
                self.schemas = tools or []
                yield ToolCall(
                    id="c1",
                    name="create_story",
                    arguments=json.dumps(
                        {
                            "title": "Child one",
                            "description": "do it",
                            "depends_on": [self.prep_id],
                        }
                    ),
                )
                return
            async for item in super().stream_chat(
                model=model, messages=messages, tools=tools,
                max_output_tokens=max_output_tokens,
            ):
                yield item

    gw = PrepGateway()
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "prep.db"))
    with TestClient(create_app(gateway=gw)) as client:
        client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
        epic = client.post("/api/v1/epics", json={"title": "e"}).json()
        project = client.post(
            "/api/v1/projects", json={"name": "p", "root": str(tmp_path)}
        ).json()
        prep = create_story(
            client, kind="prep", epic_id=epic["id"], project_id=project["id"]
        )
        gw.prep_id = prep["id"]
        client.post(f"/api/v1/stories/{prep['id']}/queue")
        done = wait_for_status(client, prep["id"], "human_review")
        assert "create_story" in [t["function"]["name"] for t in gw.schemas]

        children = [
            s
            for s in client.get("/api/v1/stories").json()
            if prep["id"] in (s.get("depends_on") or [])
        ]
        assert len(children) == 1
        assert children[0]["epic_id"] == epic["id"]
        assert children[0]["project_id"] == project["id"]
        assert children[0]["id"] in done["spec"]

        final = client.post(f"/api/v1/stories/{prep['id']}/approve", json={"by": "t"})
        assert final.status_code == 200, final.text
        assert final.json()["status"] == "done"


def test_code_story_spec_writer_has_no_create_story(client, gateway, monkeypatch):
    from app.agent_tools import ToolBox

    seen: list[dict] = []
    real = ToolBox.schemas

    def spy(self):
        out = real(self)
        seen.append({"prep": self.prep_parent_id, "names": [t["function"]["name"] for t in out]})
        return out

    monkeypatch.setattr(ToolBox, "schemas", spy)
    story = create_story(client)
    client.post(f"/api/v1/stories/{story['id']}/queue")
    wait_for_status(client, story["id"], "human_review")
    assert seen and all("create_story" not in s["names"] for s in seen)
    assert all(s["prep"] is None for s in seen)
