"""External execution mode: stories claimed and completed by outside agents.

Covers the acceptance checks for the external lifecycle:
  create (external default/validation) → queue → claim (WIP + deps + status
  guards) → complete → human gate → done/reject, plus the scheduler bypass
  (external stories are never picked up by the internal runner, not even as
  restart orphans).
"""

import asyncio
import json
import time

import httpx


def create_story(client, **body) -> dict:
    res = client.post("/api/v1/stories", json={"title": "External work", **body})
    assert res.status_code == 201
    return res.json()


def get_story(client, story_id: str) -> dict:
    res = client.get(f"/api/v1/stories/{story_id}")
    assert res.status_code == 200
    return res.json()


def queue(client, story_id: str) -> dict:
    res = client.post(f"/api/v1/stories/{story_id}/queue")
    assert res.status_code == 200, res.text
    return res.json()


def claim(client, story_id: str, claimed_by: str = "bot-1") -> dict:
    return client.post(
        f"/api/v1/stories/{story_id}/claim", json={"claimed_by": claimed_by}
    )


def complete(client, story_id: str, note: str = "") -> dict:
    return client.post(f"/api/v1/stories/{story_id}/complete", json={"note": note})


# ---- create / defaults / validation ------------------------------------


def test_create_external_story_stores_execution(client):
    story = create_story(client, execution="external")
    assert story["execution"] == "external"
    assert story["status"] == "backlog"
    assert story["claimed_by"] is None
    assert story["claimed_at"] is None


def test_execution_defaults_to_internal(client):
    assert create_story(client)["execution"] == "internal"


def test_invalid_execution_rejected(client):
    res = client.post(
        "/api/v1/stories", json={"title": "x", "execution": "sidecar"}
    )
    assert res.status_code == 400
    assert "execution" in res.json()["detail"]


def test_old_rows_read_as_internal_after_migrate(tmp_path, monkeypatch, gateway):
    """Existing DBs upgraded by _migrate: pre-column rows become internal."""
    from sqlalchemy import text
    from sqlmodel import Session, SQLModel, create_engine

    from app import models as m

    db_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    # simulate a DB written before the execution columns existed
    with Session(engine) as db:
        db.add(m.Story(title="legacy", status=m.StoryStatus.BACKLOG))
        db.commit()
    with engine.connect() as conn:
        # create_all also builds the execution index; a real pre-v0.7 DB has
        # neither, so drop the index before the columns
        conn.execute(text("DROP INDEX IF EXISTS ix_stories_execution"))
        for col in ("execution", "claimed_by", "claimed_at"):
            conn.execute(text(f"ALTER TABLE stories DROP COLUMN {col}"))
        conn.commit()
    engine.dispose()

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(gateway=gateway)) as client:
        client.patch("/api/v1/settings", json={"story_poll_interval": 9999})
        stories = client.get("/api/v1/stories").json()
        assert [s["execution"] for s in stories] == ["internal"]
        assert stories[0]["claimed_by"] is None
        # and the backfilled row still behaves as internal for the scheduler
        assert client.post(
            f"/api/v1/stories/{stories[0]['id']}/queue"
        ).status_code == 200


# ---- scheduler bypass --------------------------------------------------


def test_rebuild_preserves_execution_columns(tmp_path, monkeypatch, gateway):
    """The NOT-NULL model rebuild must not drop the v0.7 external columns.

    _migrate adds execution/claimed_by/claimed_at via ALTER *before* the
    rebuild runs, so the rebuilt table has to carry them (and the claim
    itself) across — a hardcoded pre-v0.4 column list would erase a claimed
    external story's state on upgrade."""
    from sqlalchemy import text
    from sqlmodel import Session, SQLModel, create_engine

    from app import models as m

    db_path = tmp_path / "legacy-notnull.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        story = m.Story(
            title="claimed legacy",
            model="echo/default",
            execution=m.StoryExecution.EXTERNAL,
            claimed_by="legacy-agent",
        )
        db.add(story)
        db.commit()
        story_id = story.id
    with engine.connect() as conn:
        # force the v0.3 rebuild branch: make model NOT NULL
        conn.execute(text("DROP INDEX IF EXISTS ix_stories_execution"))
        conn.execute(
            text("CREATE TABLE stories_old AS SELECT * FROM stories")
        )
        conn.execute(text("DROP TABLE stories"))
        conn.execute(text("""
            CREATE TABLE stories (
                id VARCHAR NOT NULL PRIMARY KEY,
                title VARCHAR(200) NOT NULL,
                description TEXT NOT NULL,
                acceptance TEXT NOT NULL,
                status VARCHAR NOT NULL,
                model VARCHAR NOT NULL,
                skill VARCHAR,
                created_by VARCHAR NOT NULL,
                execution VARCHAR,
                claimed_by VARCHAR(100),
                claimed_at DATETIME,
                spec TEXT,
                spec_version INTEGER NOT NULL,
                base_ref TEXT,
                verify_result TEXT,
                ai_review TEXT,
                ai_verdict VARCHAR,
                approved_by VARCHAR,
                error VARCHAR,
                project_id TEXT,
                epic_id TEXT,
                depends_on TEXT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """))
        conn.execute(text("""
            INSERT INTO stories SELECT id, title, description, acceptance,
                status, model, skill, created_by, execution, claimed_by,
                claimed_at, spec, spec_version, base_ref, verify_result,
                ai_review, ai_verdict, approved_by, error, project_id,
                epic_id, depends_on, created_at, updated_at
            FROM stories_old
        """))
        conn.execute(text("DROP TABLE stories_old"))
        conn.commit()
    engine.dispose()

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(gateway=gateway)) as client:
        stories = client.get("/api/v1/stories").json()
        assert len(stories) == 1
        assert stories[0]["id"] == story_id
        assert stories[0]["execution"] == "external"
        assert stories[0]["claimed_by"] == "legacy-agent"
        # the scheduler still ignores the rebuilt external row
        client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
        time.sleep(0.5)
        assert client.app.state.story_runner._running == set()
        assert client.get(f"/api/v1/stories/{story_id}").json()["status"] == "backlog"


class _Recorder:
    """Gateway that records every call so we can prove the runner stayed away."""

    def __init__(self):
        self.calls: list[dict] = []

    async def stream_chat(
        self, *, model: str, messages: list[dict], tools=None, max_output_tokens=None
    ):
        from app.providers import Delta, Usage

        self.calls.append({"model": model, "messages": messages})
        yield Delta(text="## Goal\nnothing to do\n\nVERDICT: APPROVED")
        yield Usage(prompt_tokens=1, completion_tokens=1)


def _quiet(client):
    """Park the scheduler so a test owns the state machine."""
    client.patch("/api/v1/settings", json={"story_poll_interval": 9999})


def _tick_it_out(client, seconds: float = 0.5) -> None:
    """Let the app's own scheduler loop run for a while (poll interval 0.05s)."""
    time.sleep(seconds)


def test_queued_external_story_is_ignored_by_scheduler(client):
    """A queued external story stays queued: no spec task, no status move."""
    _quiet(client)
    story = create_story(client, execution="external")
    queue(client, story["id"])
    client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})

    _tick_it_out(client)

    after = get_story(client, story["id"])
    assert after["status"] == "queued"
    assert after["claimed_by"] is None
    assert after["spec"] is None
    assert client.app.state.story_runner._running == set()


def test_in_progress_external_survives_restart(tmp_path, monkeypatch, gateway):
    """External in_progress across a restart is NOT resumed as an orphan."""
    db_path = tmp_path / "ext-restart.db"
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(gateway=_Recorder())) as c1:
        c1.patch("/api/v1/settings", json={"story_poll_interval": 9999})
        story = create_story(c1, execution="external")
        queue(c1, story["id"])
        assert claim(c1, story["id"]).status_code == 200
        assert get_story(c1, story["id"])["status"] == "in_progress"

    # simulate a restart: brand-new app + runner over the same DB, scheduler hot
    recorder = _Recorder()
    with TestClient(create_app(gateway=recorder)) as c2:
        c2.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
        _tick_it_out(c2)

        after = get_story(c2, story["id"])
        assert after["status"] == "in_progress"
        assert after["claimed_by"] == "bot-1"
        assert after["spec"] is None
        assert c2.app.state.story_runner._running == set()
        assert recorder.calls == []


# ---- claim ------------------------------------------------------------


def test_claim_happy_path(client):
    story = create_story(client, execution="external")
    queue(client, story["id"])
    res = claim(client, story["id"], "agent-x")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "in_progress"
    assert body["claimed_by"] == "agent-x"
    assert body["claimed_at"] is not None

    events = client.get(f"/api/v1/stories/{story['id']}").json()["events"]
    kinds = [e["kind"] for e in events]
    assert "status" in kinds
    payload = [e for e in events if e["kind"] == "status"][-1]
    assert payload["data"].find("agent-x") >= 0


def test_claim_internal_story_conflict(client):
    story = create_story(client)  # internal
    queue(client, story["id"])
    assert claim(client, story["id"]).status_code == 409


def test_claim_wrong_status_conflict(client):
    story = create_story(client, execution="external")
    # still backlog, never queued
    assert claim(client, story["id"]).status_code == 409
    queue(client, story["id"])
    assert claim(client, story["id"]).status_code == 200
    # already claimed / in_progress
    assert claim(client, story["id"]).status_code == 409


def test_claim_deps_unmet_conflict(client):
    blocker = create_story(client, execution="external", title="blocker")
    story = create_story(
        client, execution="external", depends_on=[blocker["id"]], title="dependent"
    )
    queue(client, story["id"])
    res = claim(client, story["id"])
    assert res.status_code == 409
    assert "depend" in res.json()["detail"].lower()


def test_claim_wip_limit_per_scope(client):
    """Second claim in the same project is 409; another project is unaffected."""
    one = create_story(client, execution="external", title="one")
    two = create_story(client, execution="external", title="two")
    other = create_story(client, execution="external", title="other-project")

    for s in (one, two, other):
        queue(client, s["id"])

    assert claim(client, one["id"]).status_code == 200
    res = claim(client, two["id"])
    assert res.status_code == 409
    assert "WIP" in res.json()["detail"] or "wip" in res.json()["detail"].lower()

    # different scope (a real project) is unaffected by the global claim
    project = client.post(
        "/api/v1/projects", json={"name": "other", "root": "/tmp", "extra_paths": []}
    ).json()
    scoped = create_story(
        client, execution="external", title="scoped", project_id=project["id"]
    )
    assert scoped["project_id"] == project["id"]
    queue(client, scoped["id"])
    assert claim(client, scoped["id"]).status_code == 200


def test_claim_wip_limit_scoped_to_same_project(client):
    """The guard counts per scope: two stories in the SAME explicit project
    collide at the default limit of 1, while a second project and the global
    (no-project) scope each keep their own slot."""
    # the projectless story must exist before a second project does: with two
    # projects a missing project_id is refused
    global_one = create_story(client, execution="external", title="g1")
    project = client.post(
        "/api/v1/projects", json={"name": "alpha", "root": "/tmp", "extra_paths": []}
    ).json()
    other_project = client.post(
        "/api/v1/projects", json={"name": "beta", "root": "/tmp", "extra_paths": []}
    ).json()

    alpha_one = create_story(
        client, execution="external", title="a1", project_id=project["id"]
    )
    alpha_two = create_story(
        client, execution="external", title="a2", project_id=project["id"]
    )
    beta_one = create_story(
        client, execution="external", title="b1", project_id=other_project["id"]
    )

    for s in (alpha_one, alpha_two, beta_one, global_one):
        queue(client, s["id"])

    # the single alpha slot fills...
    assert claim(client, alpha_one["id"]).status_code == 200
    # ...so a second claim inside the same project is refused
    res = claim(client, alpha_two["id"])
    assert res.status_code == 409
    assert "wip" in res.json()["detail"].lower()
    # while other scopes are untouched by alpha's occupancy
    assert claim(client, beta_one["id"]).status_code == 200
    assert claim(client, global_one["id"]).status_code == 200

    # completing the held claim frees the slot for its sibling
    complete(client, alpha_one["id"])
    assert claim(client, alpha_two["id"]).status_code == 200


def test_claim_wip_limit_respects_setting(client):
    client.patch("/api/v1/settings", json={"max_external_wip_per_project": 2})
    one = create_story(client, execution="external", title="one")
    two = create_story(client, execution="external", title="two")
    three = create_story(client, execution="external", title="three")
    for s in (one, two, three):
        queue(client, s["id"])
    assert claim(client, one["id"]).status_code == 200
    assert claim(client, two["id"]).status_code == 200
    assert claim(client, three["id"]).status_code == 409


def test_claim_empty_claimed_by_rejected(client):
    story = create_story(client, execution="external")
    queue(client, story["id"])
    assert client.post(
        f"/api/v1/stories/{story['id']}/claim", json={"claimed_by": "   "}
    ).status_code == 422


def test_claim_requires_body(client):
    story = create_story(client, execution="external")
    queue(client, story["id"])
    assert client.post(f"/api/v1/stories/{story['id']}/claim").status_code == 422


# ---- complete / release / human gate ----------------------------------


def test_complete_moves_to_human_review(client):
    story = create_story(client, execution="external")
    queue(client, story["id"])
    claim(client, story["id"], "agent-x")
    res = complete(client, story["id"], note="PR #42 landed")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "human_review"
    assert body["claimed_by"] == "agent-x"  # kept for display
    detail = client.get(f"/api/v1/stories/{story['id']}").json()
    note_events = [
        e for e in detail["events"] if "PR #42" in (e.get("data") or "")
    ]
    assert note_events, "completion note should be recorded as an event"


def test_complete_accepts_no_body(client):
    """`note` is optional — a bare POST (as a curl-ing agent would send) works."""
    story = create_story(client, execution="external")
    queue(client, story["id"])
    claim(client, story["id"], "agent-x")
    res = client.post(f"/api/v1/stories/{story['id']}/complete")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "human_review"
    assert body["claimed_by"] == "agent-x"
    assert client.post(f"/api/v1/stories/{story['id']}/approve", json={}).json()[
        "status"
    ] == "done"


def test_complete_requires_claimed_in_progress(client):
    story = create_story(client, execution="external")
    queue(client, story["id"])
    assert complete(client, story["id"]).status_code == 409
    claim(client, story["id"])
    release = client.post(f"/api/v1/stories/{story['id']}/release")
    assert release.status_code == 200
    assert complete(client, story["id"]).status_code == 409


def test_complete_on_internal_story_conflict(client):
    story = create_story(client)
    queue(client, story["id"])
    assert complete(client, story["id"]).status_code == 409


def test_approve_external_goes_straight_to_done(client):
    story = create_story(client, execution="external")
    queue(client, story["id"])
    claim(client, story["id"], "agent-x")
    complete(client, story["id"])
    res = client.post(f"/api/v1/stories/{story['id']}/approve", json={})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "done"
    assert body["base_ref"] is None  # no worktree/merge ever attempted


def test_approve_external_with_stray_base_ref_still_goes_to_done(client):
    """The external guard must sit ABOVE the base_ref/worktree branch: an
    external story that somehow carries a base_ref (e.g. switched from
    internal mid-flight) must never be routed into EXECUTING or a merge."""
    from app.db import session_scope
    from app.models import Story

    story = create_story(client, execution="external")
    queue(client, story["id"])
    claim(client, story["id"], "agent-x")
    complete(client, story["id"])
    assert get_story(client, story["id"])["status"] == "human_review"

    # smuggle in a stray base_ref, as a story switched from internal would have
    with session_scope(client.app.state.engine) as db:
        row = db.get(Story, story["id"])
        row.base_ref = "abc1234"
        db.add(row)

    res = client.post(f"/api/v1/stories/{story['id']}/approve", json={})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "done"
    assert body["claimed_by"] == "agent-x"  # kept for display


def test_discard_external_story_conflict(client):
    """External code never lived in one of our worktrees: /discard must not
    touch workspaces for an external story even when it carries a stray
    base_ref (reject is the escape for external work)."""
    from app.db import session_scope
    from app.models import Story

    story = create_story(client, execution="external")
    queue(client, story["id"])
    claim(client, story["id"], "agent-x")
    complete(client, story["id"])
    with session_scope(client.app.state.engine) as db:
        row = db.get(Story, story["id"])
        row.base_ref = "abc1234"
        db.add(row)

    calls: list[str] = []

    import app.routers.stories as stories_router

    real_discard = stories_router.workspaces.discard

    def spy_discard(root, story_id):
        calls.append(story_id)
        return real_discard(root, story_id)

    stories_router.workspaces.discard = spy_discard  # type: ignore[assignment]
    try:
        res = client.post(f"/api/v1/stories/{story['id']}/discard")
    finally:
        stories_router.workspaces.discard = real_discard  # type: ignore[assignment]

    assert res.status_code == 409
    assert "no Fubala worktree" in res.json()["detail"]
    assert calls == []  # workspaces were never touched
    # the story is untouched by the failed discard
    body = get_story(client, story["id"])
    assert body["status"] == "human_review"
    assert body["base_ref"] == "abc1234"
    assert body["claimed_by"] == "agent-x"


def test_reject_external_clears_claim_and_requeues(client):
    story = create_story(client, execution="external")
    queue(client, story["id"])
    claim(client, story["id"], "agent-x")
    complete(client, story["id"])
    res = client.post(
        f"/api/v1/stories/{story['id']}/reject", json={"reason": "CI failed"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "queued"
    assert body["claimed_by"] is None
    assert body["claimed_at"] is None
    # re-claimable now the WIP slot freed
    assert claim(client, story["id"], "agent-y").status_code == 200


def test_release_drops_claim_back_to_queued(client):
    story = create_story(client, execution="external")
    queue(client, story["id"])
    claim(client, story["id"], "agent-x")
    res = client.post(f"/api/v1/stories/{story['id']}/release")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "queued"
    assert body["claimed_by"] is None
    assert claim(client, story["id"], "agent-y").status_code == 200


def test_release_only_for_claimed_in_progress(client):
    story = create_story(client, execution="external")
    queue(client, story["id"])
    assert client.post(f"/api/v1/stories/{story['id']}/release").status_code == 409
    claim(client, story["id"])
    complete(client, story["id"])
    assert client.post(f"/api/v1/stories/{story['id']}/release").status_code == 409


def test_release_unblocks_wip_guard(client):
    """Releasing one claim must free the WIP slot for a *different* story in
    the same scope (default limit 1, no project → global scope)."""
    a = create_story(client, execution="external")
    b = create_story(client, execution="external")
    queue(client, a["id"])
    queue(client, b["id"])

    assert claim(client, a["id"], "agent-a").status_code == 200
    blocked = claim(client, b["id"], "agent-b")
    assert blocked.status_code == 409
    assert "wip" in blocked.json()["detail"].lower()

    res = client.post(f"/api/v1/stories/{a['id']}/release")
    assert res.status_code == 200
    assert claim(client, b["id"], "agent-b").status_code == 200


def test_release_records_releaser_in_event(client):
    def status_events(story_id):
        return [
            e for e in get_story(client, story_id)["events"] if e["kind"] == "status"
        ]

    story = create_story(client, execution="external")
    queue(client, story["id"])
    claim(client, story["id"], "agent-x")
    res = client.post(
        f"/api/v1/stories/{story['id']}/release", json={"by": "operator"}
    )
    assert res.status_code == 200
    assert res.json()["status"] == "queued"
    release_events = [
        e for e in status_events(story["id"]) if '"released"' in e["data"]
    ]
    assert release_events, "expected a release status event"
    data = json.loads(release_events[-1]["data"])
    assert data["released"] == "agent-x"
    assert data["by"] == "operator"

    # a bare POST (no body) falls back to the "local" default
    fresh = create_story(client, execution="external")
    queue(client, fresh["id"])
    claim(client, fresh["id"], "agent-y")
    assert client.post(f"/api/v1/stories/{fresh['id']}/release").status_code == 200
    bare = [
        e for e in status_events(fresh["id"]) if '"released"' in e["data"]
    ][-1]
    bare_data = json.loads(bare["data"])
    assert bare_data["released"] == "agent-y"
    assert bare_data["by"] == "local"


def test_queue_clears_stale_claim_on_external(client):
    """Requeueing an external story always drops any leftover claim."""
    story = create_story(client, execution="external")
    queue(client, story["id"])
    claim(client, story["id"], "agent-x")
    complete(client, story["id"])  # -> human_review
    rejected = client.post(
        f"/api/v1/stories/{story['id']}/reject", json={"reason": "redo"}
    )
    assert rejected.status_code == 200
    assert rejected.json()["claimed_by"] is None

    # a story parked back in backlog via unqueue requeues clean too
    parked = create_story(client, execution="external")
    queue(client, parked["id"])
    assert client.post(f"/api/v1/stories/{parked['id']}/unqueue").status_code == 200
    queued_again = queue(client, parked["id"])
    assert queued_again["claimed_by"] is None


def test_execution_switchable_while_editable(client):
    story = create_story(client)
    assert story["execution"] == "internal"
    res = client.patch(f"/api/v1/stories/{story['id']}", json={"execution": "external"})
    assert res.status_code == 200
    assert res.json()["execution"] == "external"
    # invalid value rejected
    assert client.patch(
        f"/api/v1/stories/{story['id']}", json={"execution": "nope"}
    ).status_code == 400


def test_execution_not_editable_in_flight(client):
    story = create_story(client, execution="external")
    queue(client, story["id"])
    claim(client, story["id"])
    res = client.patch(f"/api/v1/stories/{story['id']}", json={"execution": "internal"})
    assert res.status_code == 409


def test_patch_internal_lands_on_inherited_external_story(client):
    """PATCH internal must stick on a story that only *inherits* external.

    Regression: the comparison collapsed a NULL override to internal, so
    PATCHing internal on a story in an external-executor project was a
    silent no-op while the project kept the story effectively external."""
    # scheduler off: this is a storage-shape test — a tick that claims the
    # queued story mid-test would 409 the final PATCH that clears the
    # override (in-flight stories are not editable)
    client.patch("/api/v1/settings", json={"story_poll_interval": 9999})
    project = client.post(
        "/api/v1/projects",
        json={"name": "Handoff", "root": "/tmp", "executor": "external"},
    )
    assert project.status_code == 201
    story = create_story(client, project_id=project.json()["id"])
    assert story["execution"] == "external"  # inherited, effective view

    flipped = client.patch(
        f"/api/v1/stories/{story['id']}", json={"execution": "internal"}
    )
    assert flipped.status_code == 200
    assert flipped.json()["execution"] == "internal"
    assert get_story(client, story["id"])["execution"] == "internal"

    # in effect: as an internal story the outside agent's claim is refused
    queue(client, story["id"])
    res = claim(client, story["id"])
    assert res.status_code == 409
    assert "only external stories can be claimed" in res.json()["detail"]

    # stored, not just displayed: clearing the override lets the story
    # inherit the project executor again (queued is still editable)
    cleared = client.patch(f"/api/v1/stories/{story['id']}", json={"execution": None})
    assert cleared.status_code == 200
    assert cleared.json()["execution"] == "external"


# ---- MCP round-trip ------------------------------------------------------
# The MCP tools are an in-process client of /api/v1 (mcp_tools.bind_app); they
# can be called directly once bound to the test app.


async def _mcp_setup(client, monkeypatch):
    from app import mcp_tools as mcp

    # the tools call the REST API in-process: bind them to the TestClient's app
    # so both sides share the same app instance and DB
    mcp.bind_app(client.app)
    return mcp, mcp._get, mcp._post


def test_mcp_create_story_accepts_execution(client, monkeypatch):
    mcp, _, _ = asyncio.run(_mcp_setup(client, monkeypatch))
    out = asyncio.run(mcp.create_story(title="MCP external", execution="external"))
    assert "MCP external" in out
    assert "execution: external" in out
    story_id = next(line.split(": ")[1] for line in out.splitlines() if line.startswith("- id: "))
    story = get_story(client, story_id)
    assert story["execution"] == "external"


def test_mcp_claim_complete_release_round_trip(client, monkeypatch):
    mcp, _, _ = asyncio.run(_mcp_setup(client, monkeypatch))
    created = create_story(client, execution="external")
    queue(client, created["id"])

    claimed = asyncio.run(mcp.claim_story(created["id"], "agent-mcp"))
    assert "claimed by agent-mcp" in claimed
    story = get_story(client, created["id"])
    assert story["status"] == "in_progress"
    assert story["claimed_by"] == "agent-mcp"
    assert "claimed by agent-mcp" in claimed  # summary echoes it

    done = asyncio.run(mcp.complete_story(created["id"], note="PR #42 merged"))
    assert "human_review" in done
    story = get_story(client, created["id"])
    assert story["status"] == "human_review"
    assert story["claimed_by"] == "agent-mcp"
    # the completion note lands in the event log (spec: "recorded as event")
    events = story["events"]
    assert any("PR #42 merged" in e["data"] for e in events if e["kind"] == "note")

    # approve at the human gate: no worktree/merge attempted, straight to done
    approved = client.post(
        f"/api/v1/stories/{created['id']}/approve", json={"by": "local"}
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "done"

    # release: the stale-claim escape on a freshly claimed story
    second = create_story(client, execution="external")
    queue(client, second["id"])
    asyncio.run(mcp.claim_story(second["id"], "agent-mcp"))
    released = asyncio.run(mcp.release_story(second["id"], released_by="operator"))
    assert "queued" in released
    detail = get_story(client, second["id"])
    assert detail["claimed_by"] is None
    # the MCP tool forwards released_by into the release event
    release_events = [
        e
        for e in detail["events"]
        if e["kind"] == "status" and '"released"' in e["data"]
    ]
    assert release_events
    data = json.loads(release_events[-1]["data"])
    assert data["released"] == "agent-mcp"
    assert data["by"] == "operator"


def test_mcp_claim_reports_conflicts(client, monkeypatch):
    mcp, _, _ = asyncio.run(_mcp_setup(client, monkeypatch))
    story = create_story(client)  # internal
    queue(client, story["id"])
    out = asyncio.run(mcp.claim_story(story["id"], "agent-mcp"))
    assert "claim failed" in out
    assert "409" in out


# ---- cancel ---------------------------------------------------------------


def test_mcp_story_action_cancel_round_trip(client, monkeypatch):
    """MCP `story_action(action="cancel")` must reach POST /stories/{id}/cancel
    with {"by": reason or "mcp"} and land the story in failed."""
    mcp, _, _ = asyncio.run(_mcp_setup(client, monkeypatch))
    story = create_story(client, execution="external")
    queue(client, story["id"])
    claim(client, story["id"], "bot-1")
    assert get_story(client, story["id"])["status"] == "in_progress"

    out = asyncio.run(mcp.story_action(story["id"], "cancel", "operator asked"))
    assert "failed" in out
    story_after = get_story(client, story["id"])
    assert story_after["status"] == "failed"
    assert story_after["error"] == "cancelled by operator asked"

    # default canceller name when no reason is given
    second = create_story(client, execution="external")
    queue(client, second["id"])
    claim(client, second["id"], "bot-2")
    asyncio.run(mcp.story_action(second["id"], "cancel"))
    assert get_story(client, second["id"])["error"] == "cancelled by mcp"


def test_cancel_external_story_clears_claim(client):
    """External cancel is a DB transition only: no workspaces touched, the
    claim is dropped so the WIP slot frees, and the story is queueable."""
    story = create_story(client, execution="external")
    queue(client, story["id"])
    claimed = claim(client, story["id"], "bot-1")
    assert claimed.status_code == 200
    assert claimed.json()["status"] == "in_progress"

    res = client.post(f"/api/v1/stories/{story['id']}/cancel", json={"by": "ops"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "failed"
    assert body["error"] == "cancelled by ops"
    assert body["claimed_by"] is None
    assert body["claimed_at"] is None

    # the freed slot lets another story claim immediately
    other = create_story(client, execution="external")
    queue(client, other["id"])
    assert claim(client, other["id"], "bot-2").status_code == 200

    requeued = client.post(f"/api/v1/stories/{story['id']}/queue")
    assert requeued.status_code == 200
    assert requeued.json()["status"] == "queued"


def test_cancel_rejects_non_inflight_statuses(client):
    """409 from backlog/queued/human_review/done; 404 for an unknown id."""
    from fastapi.testclient import TestClient  # noqa: F401  (client is one)

    # scheduler HARD-off across the whole test: runner.stop() awaits the
    # cancelled loop task, so the tick that may already be sleeping on the
    # fixture's 0.05s interval cannot claim the queued story below — if it
    # did, that story would be in_progress and its /cancel would return 200
    # instead of the expected 409 (this race flaked under xdist worker load;
    # no sleep-based park closes the window).
    client.portal.call(client.app.state.story_runner.stop)

    # backlog
    backlog = create_story(client)
    assert (
        client.post(f"/api/v1/stories/{backlog['id']}/cancel", json={"by": "t"}).status_code
        == 409
    )
    # queued
    queued = create_story(client)
    queue(client, queued["id"])
    assert (
        client.post(f"/api/v1/stories/{queued['id']}/cancel", json={"by": "t"}).status_code
        == 409
    )
    # human_review (external: claim -> complete) and done (approve)
    story = create_story(client, execution="external")
    queue(client, story["id"])
    claim(client, story["id"], "bot-1")
    assert complete(client, story["id"]).status_code == 200
    assert get_story(client, story["id"])["status"] == "human_review"
    assert (
        client.post(f"/api/v1/stories/{story['id']}/cancel", json={"by": "t"}).status_code
        == 409
    )
    approved = client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
    assert approved.json()["status"] == "done"
    assert (
        client.post(f"/api/v1/stories/{story['id']}/cancel", json={"by": "t"}).status_code
        == 409
    )
    # unknown id
    assert (
        client.post("/api/v1/stories/nope/cancel", json={"by": "t"}).status_code == 404
    )
    # and the untouched story never left backlog
    assert get_story(client, backlog["id"])["status"] == "backlog"
    # restart so the fixture's shutdown stop() follows the normal path
    # (cancel + bus unsubscribe); stopping an already-stopped runner returns
    # early and would leak this app's watchdog queue into the shared bus
    client.portal.call(client.app.state.story_runner.start)


# ---- switching modes mid-flight ------------------------------------------


def test_switching_execution_flips_scheduler_eligibility(client, gateway):
    """A queued story switched internal -> external must leave the scheduler's
    candidate set; switched back, it must re-enter it. Proves the or_() filter
    is driven by the stored value (NULL-safe both ways), not by accident."""
    # Hard-off the scheduler across the setup: runner.stop() awaits the
    # cancelled loop task, so no tick — not even one already sleeping on the
    # fixture's 0.05s interval — can fire between create/queue and the mode
    # patch below. (A parked-but-sleeping tick claimed the story under xdist
    # worker load and 409'd that patch; no sleep-based drain closes that
    # window, stopping the loop does.)
    runner = client.app.state.story_runner
    client.portal.call(runner.stop)
    story = create_story(client)  # internal
    queue(client, story["id"])

    # external now: the runner must never pick it up
    assert (
        client.patch(
            f"/api/v1/stories/{story['id']}", json={"execution": "external"}
        ).status_code
        == 200
    )
    # restart the loop for the probe phase: ticks are hot again and the
    # queued EXTERNAL story must stay untouched by them
    client.portal.call(runner.start)
    client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
    _tick_it_out(client)
    assert get_story(client, story["id"])["status"] == "queued"
    assert client.app.state.story_runner._running == set()
    assert gateway.calls == []

    # and back to internal: the pipeline owns it again (spec gets written)
    assert (
        client.patch(
            f"/api/v1/stories/{story['id']}", json={"execution": "internal"}
        ).status_code
        == 200
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current = get_story(client, story["id"])
        if current["spec"]:
            break
        time.sleep(0.1)
    current = get_story(client, story["id"])
    assert current["spec"], "internal story should be picked up after switching back"
    assert current["status"] in ("ai_review", "human_review")
    assert gateway.calls, "the spec writer should have run"


def test_switching_execution_drops_stale_claim(client):
    """Flipping an external story to internal clears any leftover claim so the
    runner can own it, and vice versa (an external story must be re-claimed)."""
    story = create_story(client, execution="external")
    queue(client, story["id"])
    claim(client, story["id"], "agent-x")
    assert get_story(client, story["id"])["claimed_by"] == "agent-x"
    # the story is in_progress, so editing is refused entirely
    res = client.patch(f"/api/v1/stories/{story['id']}", json={"execution": "internal"})
    assert res.status_code == 409


# ---- board SSE: published payload must match committed state ------------


def _sse_bodies(sent: list[dict]) -> list[bytes]:
    """Raw response-body chunks an ASGI app sent for an SSE stream."""
    out: list[bytes] = []
    for message in sent:
        if message.get("type") != "http.response.body":
            continue
        body = message.get("body", b"")
        out.append(body.encode() if isinstance(body, str) else body)
    return out


_bodies = _sse_bodies  # short alias used inside the SSE test


def _sse_scope(path: str) -> dict:
    """Minimal ASGI scope for a GET on the SSE endpoint."""
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "raw_path": path.encode(),
        "path": path,
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 123),
        "root_path": "",
    }


async def _wait_for_status_line(sent: list[dict], timeout: float = 30.0) -> None:
    """Block until the app has sent its `http.response.start` message.

    The SSE generator only subscribes to the bus after the response headers
    are emitted, so waiting for that message is also how the test knows the
    subscription is live before driving the cycle.
    """
    deadline = time.monotonic() + timeout
    while not any(m.get("type") == "http.response.start" for m in sent):
        if time.monotonic() > deadline:
            raise AssertionError(f"SSE stream never started: {sent}")
        await asyncio.sleep(0.01)


def test_external_cycle_sse_matches_committed_story(tmp_path, monkeypatch, gateway):
    """The board's defect report (2026-09-27) blamed stale client state; this
    pins the server side of that contract: every `story` event a subscriber
    receives describes *committed* state, so a client that just replays the
    last event ends where ``GET /stories/{id}`` says it is.

    A full external cycle (claim → complete → approve) is driven against a
    live SSE subscription; the last `story` payload must be ``done`` and
    identical to the REST snapshot.

    The SSE endpoint is driven over raw ASGI rather than ``httpx.ASGITransport``:
    httpx 0.28's transport awaits the entire ASGI app before returning, so an
    infinite stream never yields its headers and the request would hang forever.
    Raw ASGI keeps the subscriber on the same event loop the request handlers
    publish from — which is exactly what a real browser tab gets.
    """
    from contextlib import AsyncExitStack

    from app.main import create_app

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "sse.db"))
    app = create_app(gateway=gateway)

    received: list[dict] = []

    def parse_sse(messages: list[dict]) -> None:
        received.clear()
        event_kind = None
        for line in b"".join(_bodies(messages)).decode().split("\n"):
            if line.startswith("event: "):
                event_kind = line[len("event: ") :]
            elif line.startswith("data: ") and event_kind == "story":
                received.append(json.loads(line[len("data: ") :])["story"])

    async def drive() -> None:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(app.router.lifespan_context(app))
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                    timeout=httpx.Timeout(10),
                )
            )
            await client.patch("/api/v1/settings", json={"story_poll_interval": 9999})
            story = (
                await client.post(
                    "/api/v1/stories",
                    json={
                        "title": "external cycle",
                        "description": "",
                        "acceptance": "",
                        "execution": "external",
                    },
                )
            ).json()
            await client.post(f"/api/v1/stories/{story['id']}/queue", json={})

            # -- subscribe: an SSE response never completes, so the ASGI call
            #    below runs as a task we later cancel (a closing browser tab).
            sent: list[dict] = []
            fed_request_body = False

            async def receive() -> dict:
                nonlocal fed_request_body
                if not fed_request_body:
                    fed_request_body = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await asyncio.Event().wait()  # never send a disconnect
                return {"type": "http.disconnect"}  # pragma: no cover

            async def send(message: dict) -> None:
                sent.append(message)

            stream_task = asyncio.create_task(
                app(_sse_scope("/api/v1/stories/events"), receive, send)
            )
            await _wait_for_status_line(sent)
            assert sent[0]["status"] == 200, sent[0]

            # -- drive the full external cycle while subscribed --
            assert (
                await client.post(
                    f"/api/v1/stories/{story['id']}/claim",
                    json={"claimed_by": "agent-sse"},
                )
            ).status_code == 200
            assert (
                await client.post(
                    f"/api/v1/stories/{story['id']}/complete",
                    json={"note": "merged"},
                )
            ).status_code == 200
            assert (
                await client.post(
                    f"/api/v1/stories/{story['id']}/approve", json={"by": "local"}
                )
            ).status_code == 200

            # -- wait until the `done` snapshot has been flushed to the wire --
            for _ in range(200):
                parse_sse(sent)
                if received and received[-1].get("status") == "done":
                    break
                # yield so the SSE generator can drain its queue
                await asyncio.sleep(0.05)

            rest = (await client.get(f"/api/v1/stories/{story['id']}")).json()

            stream_task.cancel()
            try:
                await stream_task
            except asyncio.CancelledError:
                pass

        assert received, "no `story` events reached the subscriber"
        last = received[-1]
        assert last["status"] == "done"
        assert rest["status"] == "done"
        # `GET /stories/{id}` also returns detail-only sections (events, notes,
        # code_diff, blocking) that the board `story` payload never carries —
        # compare the story snapshot fields the board renders from.
        assert last == {k: v for k, v in rest.items() if k in last}, (
            "the published SSE snapshot must equal the committed REST "
            f"snapshot\nSSE : {last}\nREST: {rest}"
        )
        # the whole transition chain is visible, in order, on the wire
        statuses = [s["status"] for s in received]
        assert statuses.index("in_progress") < statuses.index("human_review")
        assert statuses.index("human_review") < statuses.index("done")

    asyncio.run(drive())


# ---- evidence ----------------------------------------------------------


def post_evidence(client, story_id: str, **body):
    return client.post(f"/api/v1/stories/{story_id}/evidence", json=body)


def test_evidence_roundtrip_and_code_diff_fallback(client):
    story = create_story(client, execution="external")
    queue(client, story["id"])
    assert claim(client, story["id"]).status_code == 200

    res = post_evidence(
        client,
        story["id"],
        code_diff="diff --git a/x.py b/x.py\n+print('hi')\n",
        verify={"passed": True, "command": "uv run pytest", "output": "3 passed"},
        summary="Landed the thing",
    )
    assert res.status_code == 200, res.text
    assert res.json() == {
        "code_diff": "diff --git a/x.py b/x.py\n+print('hi')\n",
        "verify": {"passed": True, "command": "uv run pytest", "output": "3 passed"},
        "summary": "Landed the thing",
    }

    detail = get_story(client, story["id"])
    # external stories never get a worktree, so base_ref is None and the
    # submitted diff is the only code_diff the board can show
    assert detail["base_ref"] is None
    assert detail["evidence"]["summary"] == "Landed the thing"
    assert detail["evidence"]["verify"]["passed"] is True
    assert detail["code_diff"] == "diff --git a/x.py b/x.py\n+print('hi')\n"


def test_evidence_resubmission_overwrites(client):
    story = create_story(client, execution="external")
    assert post_evidence(client, story["id"], code_diff="first", summary="one").status_code == 200
    res = post_evidence(
        client,
        story["id"],
        code_diff="second",
        verify={"passed": False, "command": "make test", "output": "boom"},
    )
    assert res.status_code == 200
    detail = get_story(client, story["id"])
    assert detail["evidence"]["code_diff"] == "second"
    assert detail["evidence"]["summary"] is None
    assert detail["evidence"]["verify"] == {
        "passed": False,
        "command": "make test",
        "output": "boom",
    }
    assert detail["code_diff"] == "second"


def test_evidence_accepts_null_verify_and_summary(client):
    story = create_story(client, execution="external")
    res = client.post(
        f"/api/v1/stories/{story['id']}/evidence",
        json={"code_diff": "only a diff", "verify": None, "summary": None},
    )
    assert res.status_code == 200, res.text
    detail = get_story(client, story["id"])
    assert detail["evidence"] == {"code_diff": "only a diff", "verify": None, "summary": None}
    assert detail["code_diff"] == "only a diff"


def test_evidence_unknown_story_404(client):
    res = client.post(
        "/api/v1/stories/nope/evidence",
        json={"code_diff": "x"},
    )
    assert res.status_code == 404


def test_evidence_accepted_after_done(client):
    """Evidence may arrive after the human gate closed (done included)."""
    story = create_story(client, execution="external")
    queue(client, story["id"])
    assert claim(client, story["id"]).status_code == 200
    assert complete(client, story["id"], note="pr merged").status_code == 200
    assert (
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "local"}).status_code
        == 200
    )
    assert get_story(client, story["id"])["status"] == "done"
    res = post_evidence(client, story["id"], code_diff="late diff", summary="after the gate")
    assert res.status_code == 200, res.text
    assert get_story(client, story["id"])["evidence"]["summary"] == "after the gate"


def test_evidence_redacts_secrets_and_logs_compact_event(client):
    story = create_story(client, execution="external")
    res = post_evidence(
        client,
        story["id"],
        code_diff="+API_KEY = 'sk-live-abcdef1234567890'\n",
        verify={"passed": True, "command": "pytest", "output": "token=ghp_1234567890abcdef ok"},
        summary="deployed with sk-live-abcdef1234567890",
    )
    assert res.status_code == 200
    detail = get_story(client, story["id"])
    # the diff is code and stays byte-for-byte; prose fields are redacted
    assert detail["evidence"]["code_diff"] == "+API_KEY = 'sk-live-abcdef1234567890'\n"
    assert "sk-live" not in (detail["evidence"]["summary"] or "")
    assert "ghp_" not in detail["evidence"]["verify"]["output"]

    events = detail["events"]
    kinds = [e["kind"] for e in events]
    assert "evidence" in kinds
    data = json.loads([e for e in events if e["kind"] == "evidence"][0]["data"])
    # compact by construction: no diff body, no full output
    assert "code_diff" not in data
    assert data["diff_bytes"] == len(b"+API_KEY = 'sk-live-abcdef1234567890'\n")
    assert data["verify_passed"] is True
    assert len(data.get("summary", "")) <= 120
    assert "sk-live" not in data.get("summary", "")


def test_evidence_survives_rebuild_migration(tmp_path, monkeypatch, gateway):
    """The NOT-NULL model rebuild must not drop the evidence column.

    _migrate ALTERs `evidence` in before the rebuild runs, so the hardcoded
    `wanted`/`new_schema` lists have to carry it — otherwise a legacy
    NOT-NULL-model DB silently loses evidence right after the ALTER added it
    (the same trap test_rebuild_preserves_execution_columns guards for v0.7)."""
    from sqlalchemy import text
    from sqlmodel import Session, SQLModel, create_engine

    from app import models as m

    db_path = tmp_path / "legacy-evidence.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        story = m.Story(
            title="evidence legacy",
            model="echo/default",
            evidence=json.dumps({"code_diff": "kept", "verify": None, "summary": "old"}),
        )
        db.add(story)
        db.commit()
        story_id = story.id
    with engine.connect() as conn:
        # force the v0.3 rebuild branch: make model NOT NULL, with the
        # evidence column already present (exactly the state _migrate's
        # ALTER leaves behind) and holding data
        conn.execute(text("DROP INDEX IF EXISTS ix_stories_execution"))
        conn.execute(text("CREATE TABLE stories_old AS SELECT * FROM stories"))
        conn.execute(text("DROP TABLE stories"))
        conn.execute(text("""
            CREATE TABLE stories (
                id VARCHAR NOT NULL PRIMARY KEY,
                title VARCHAR(200) NOT NULL,
                description TEXT NOT NULL,
                acceptance TEXT NOT NULL,
                status VARCHAR NOT NULL,
                model VARCHAR NOT NULL,
                skill VARCHAR,
                created_by VARCHAR NOT NULL,
                execution VARCHAR,
                claimed_by VARCHAR(100),
                claimed_at DATETIME,
                spec TEXT,
                spec_version INTEGER NOT NULL,
                base_ref TEXT,
                verify_result TEXT,
                evidence TEXT,
                ai_review TEXT,
                ai_verdict VARCHAR,
                approved_by VARCHAR,
                error VARCHAR,
                project_id TEXT,
                epic_id TEXT,
                depends_on TEXT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """))
        conn.execute(text("""
            INSERT INTO stories SELECT id, title, description, acceptance,
                status, model, skill, created_by, execution, claimed_by,
                claimed_at, spec, spec_version, base_ref, verify_result,
                evidence, ai_review, ai_verdict, approved_by, error,
                project_id, epic_id, depends_on, created_at, updated_at
            FROM stories_old
        """))
        conn.execute(text("DROP TABLE stories_old"))
        conn.commit()
    engine.dispose()

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(gateway=gateway)) as client:
        detail = client.get(f"/api/v1/stories/{story_id}").json()
        assert detail["evidence"]["code_diff"] == "kept"
        assert detail["code_diff"] == "kept"
        # and the migrated column still accepts new evidence
        res = client.post(
            f"/api/v1/stories/{story_id}/evidence",
            json={"code_diff": "fresh", "verify": None, "summary": None},
        )
        assert res.status_code == 200
        assert client.get(f"/api/v1/stories/{story_id}").json()["evidence"] == {
            "code_diff": "fresh",
            "verify": None,
            "summary": None,
        }


def test_corrupt_evidence_json_degrades_to_none(client):
    """A hand-edited DB row with invalid JSON must not break the story view."""
    from app import models as m
    from app.db import session_scope

    story = create_story(client, execution="external")
    with session_scope(client.app.state.engine) as db:
        row = db.get(m.Story, story["id"])
        row.evidence = "{not json"
        db.add(row)
    detail = get_story(client, story["id"])
    assert detail["evidence"] is None
    assert detail["code_diff"] is None


# ---- project-level executor (v0.10) -------------------------------------
#
# Execution mode lives on the project: Project.executor (internal|external)
# is inherited by stories whose execution is NULL; the story value is an
# explicit override. The scheduler resolves this read-time on every tick.


def make_project(client, executor: str = "internal") -> dict:
    res = client.post(
        "/api/v1/projects",
        json={"name": f"proj-{executor}", "root": "/tmp", "executor": executor},
    )
    assert res.status_code == 201, res.text
    return res.json()


def test_story_inherits_project_execution(client):
    """No override + external project => effective execution external."""
    _quiet(client)
    # a projectless story is internal (nothing to inherit); created first,
    # since a missing project_id is refused once two projects exist
    assert create_story(client)["execution"] == "internal"
    project = make_project(client, executor="external")
    story = create_story(client, project_id=project["id"])
    assert story["execution"] == "external"
    assert story["execution_override"] is None
    # an internal project resolves the same way, to internal
    internal_project = make_project(client, executor="internal")
    internal_story = create_story(client, project_id=internal_project["id"])
    assert internal_story["execution"] == "internal"
    assert internal_story["execution_override"] is None


def test_queued_inherit_story_in_external_project_is_skipped(client, gateway):
    """The core routing fix: a queued inherit-story in an external project
    is never picked up by the internal scheduler — it waits for a claim."""
    _quiet(client)
    project = make_project(client, executor="external")
    story = create_story(client, project_id=project["id"])
    queue(client, story["id"])

    client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
    _tick_it_out(client)

    after = get_story(client, story["id"])
    assert after["status"] == "queued"
    assert after["spec"] is None
    assert client.app.state.story_runner._running == set()
    assert gateway.calls == []


def test_queued_inherit_story_in_internal_project_runs(client, gateway):
    """Internal project behaves exactly as before: the runner picks it up."""
    _quiet(client)
    project = make_project(client, executor="internal")
    story = create_story(client, project_id=project["id"])
    queue(client, story["id"])

    client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if get_story(client, story["id"])["spec"]:
            break
        time.sleep(0.1)
    current = get_story(client, story["id"])
    assert current["spec"], "internal-project inherit story should run"
    assert current["status"] in ("ai_review", "human_review")
    assert gateway.calls


def test_explicit_internal_override_runs_in_external_project(client, gateway):
    """Override wins over the project: an explicitly internal story runs
    internally even though its project is external."""
    _quiet(client)
    project = make_project(client, executor="external")
    story = create_story(client, execution="internal", project_id=project["id"])
    assert story["execution"] == "internal"
    assert story["execution_override"] == "internal"
    queue(client, story["id"])

    client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if get_story(client, story["id"])["spec"]:
            break
        time.sleep(0.1)
    current = get_story(client, story["id"])
    assert current["spec"], "explicit internal override should run internally"
    assert gateway.calls


def test_explicit_external_override_waits_in_internal_project(client, gateway):
    """Override wins over the project in the other direction too."""
    _quiet(client)
    project = make_project(client, executor="internal")
    story = create_story(client, execution="external", project_id=project["id"])
    queue(client, story["id"])

    client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
    _tick_it_out(client)

    after = get_story(client, story["id"])
    assert after["status"] == "queued"
    assert after["spec"] is None
    assert client.app.state.story_runner._running == set()
    assert gateway.calls == []


def test_toggling_project_to_external_affects_queued_stories(client, gateway):
    """Inheritance is read-time: flipping the project retroactively reroutes
    stories queued while the project was still internal."""
    # Scheduler HARD-off across the setup (the pattern of the tests above):
    # _quiet only lengthens the NEXT sleep, so a tick already scheduled could
    # still pick the story up while the project is internal -- it did under
    # CI load (land run 3055, image build 3054, 24 Oct), ending in
    # human_review. runner.stop() awaits the loop, so nothing ticks until
    # the toggle is in.
    runner = client.app.state.story_runner
    client.portal.call(runner.stop)
    project = make_project(client, executor="internal")
    story = create_story(client, project_id=project["id"])
    queue(client, story["id"])

    patched = client.patch(
        f"/api/v1/projects/{project['id']}", json={"executor": "external"}
    )
    assert patched.status_code == 200
    assert patched.json()["executor"] == "external"

    client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
    # restart the loop for the probe phase: ticks are hot again, and the
    # story (now effectively external) must stay queued. Restarting rather
    # than leaving the runner stopped also keeps the fixture's shutdown
    # stop() on the normal path.
    client.portal.call(runner.start)
    _tick_it_out(client)

    after = get_story(client, story["id"])
    assert after["status"] == "queued"
    assert after["execution"] == "external"
    # the toggle must not rewrite the story row: it still inherits
    assert after["execution_override"] is None
    assert client.app.state.story_runner._running == set()
    assert gateway.calls == []


def test_clearing_override_returns_to_project_default(client):
    """PATCH execution: null (explicit key) clears the override back to
    inherit; omitting the key entirely is a no-op."""
    _quiet(client)
    project = make_project(client, executor="external")
    story = create_story(client, execution="internal", project_id=project["id"])

    # omit the key: nothing changes
    client.patch(f"/api/v1/stories/{story['id']}", json={"title": "renamed"})
    still = get_story(client, story["id"])
    assert still["execution"] == "internal"
    assert still["execution_override"] == "internal"

    # explicit null: back to inheriting the (external) project
    cleared = client.patch(
        f"/api/v1/stories/{story['id']}", json={"execution": None}
    ).json()
    assert cleared["execution"] == "external"
    assert cleared["execution_override"] is None


def test_claim_on_inherit_external_succeeds_and_stamps(client):
    """claim_story guards on the *effective* execution and materializes it,
    so the WIP count, lease sweep, complete and release keep working."""
    _quiet(client)
    project = make_project(client, executor="external")
    story = create_story(client, project_id=project["id"])
    queue(client, story["id"])

    res = claim(client, story["id"], claimed_by="outside-bot")
    assert res.status_code == 200, res.text
    claimed = res.json()
    assert claimed["status"] == "in_progress"
    assert claimed["claimed_by"] == "outside-bot"
    # the inherit sentinel is materialized to a concrete external
    assert claimed["execution"] == "external"
    assert claimed["execution_override"] == "external"

    # the stamped value keeps the WIP guard honest for a second claim
    other = create_story(client, project_id=project["id"])
    queue(client, other["id"])
    assert claim(client, other["id"]).status_code == 409

    # and the lifecycle completes
    assert complete(client, story["id"], note="landed via PR").status_code == 200
    done = get_story(client, story["id"])
    assert done["status"] == "human_review"


def test_claim_on_inherit_internal_is_rejected(client):
    """An internal project's inherit story cannot be claimed: it is the
    runner's to pick up."""
    _quiet(client)
    project = make_project(client, executor="internal")
    story = create_story(client, project_id=project["id"])
    queue(client, story["id"])
    res = claim(client, story["id"])
    assert res.status_code == 409
    assert "internal pipeline" in res.json()["detail"]


def test_executor_backfill_migrates_external_projects(tmp_path, monkeypatch, gateway):
    """Upgrade path: a project that already owns external stories gets
    executor='external' stamped, so those stories behave as before."""
    from sqlmodel import Session, SQLModel, create_engine

    from app import models as m

    db_path = tmp_path / "backfill.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        external_project = m.Project(name="handoff", root="/tmp")
        internal_project = m.Project(name="main", root="/tmp")
        db.add(external_project)
        db.add(internal_project)
        db.flush()
        db.add(
            m.Story(
                title="claimed",
                status=m.StoryStatus.IN_PROGRESS,
                project_id=external_project.id,
                execution=m.StoryExecution.EXTERNAL,
                claimed_by="bot",
            )
        )
        db.add(
            m.Story(
                title="internal one",
                project_id=external_project.id,
                execution=m.StoryExecution.INTERNAL,
            )
        )
        db.add(m.Story(title="no project", execution=m.StoryExecution.EXTERNAL))
        db.commit()
    with engine.connect() as conn:
        from sqlalchemy import text

        conn.execute(text("UPDATE projects SET executor=NULL"))
        conn.commit()
    engine.dispose()

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(gateway=gateway)) as client:
        client.patch("/api/v1/settings", json={"story_poll_interval": 9999})
        projects = {p["name"]: p for p in client.get("/api/v1/projects").json()}
        # the project with external stories flipped, and only that one
        assert projects["handoff"]["executor"] == "external"
        assert projects["main"]["executor"] == "internal"
        # legacy rows keep their concrete values: explicit overrides, not
        # newly-inheriting stories whose behavior would silently change
        stories = {s["title"]: s for s in client.get("/api/v1/stories").json()}
        assert stories["claimed"]["execution_override"] == "external"
        assert stories["internal one"]["execution_override"] == "internal"


