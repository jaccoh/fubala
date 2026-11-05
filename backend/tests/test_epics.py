"""Epics and story dependencies."""

import time


def create_story(client, **body) -> dict:
    return client.post("/api/v1/stories", json={"title": "S", **body}).json()


def wait_for_status(client, story_id: str, want: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    story = None
    while time.time() < deadline:
        story = client.get(f"/api/v1/stories/{story_id}").json()
        if story["status"] == want:
            return story
        time.sleep(0.05)
    assert story is not None
    raise AssertionError(f"never reached {want}, last: {story['status']}")


# ---- epics ---------------------------------------------------------------


def test_epic_crud_and_story_membership(client):
    epic = client.post("/api/v1/epics", json={"title": "Auth overhaul"}).json()
    assert epic["story_count"] == 0

    story = create_story(client, epic_id=epic["id"])
    assert story["epic_id"] == epic["id"]

    listed = client.get("/api/v1/epics").json()
    assert listed[0]["story_count"] == 1 and listed[0]["done_count"] == 0

    patched = client.patch(f"/api/v1/epics/{epic['id']}", json={"title": "Auth v2"}).json()
    assert patched["title"] == "Auth v2"

    # deleting unassigns, keeps stories
    assert client.delete(f"/api/v1/epics/{epic['id']}").status_code == 204
    orphan = client.get(f"/api/v1/stories/{story['id']}").json()
    assert orphan["epic_id"] is None


# ---- dependencies ----------------------------------------------------------


def test_dependency_validation(client):
    a = create_story(client)
    b = create_story(client, depends_on=[a["id"]])
    assert b["depends_on"] == [a["id"]]

    detail = client.get(f"/api/v1/stories/{b['id']}").json()
    assert detail["blocking"][0]["id"] == a["id"]

    # unknown dep, self dep, direct cycle, transitive cycle
    res = client.post("/api/v1/stories", json={"title": "x", "depends_on": ["nope"]})
    assert res.status_code == 400
    res = client.patch(f"/api/v1/stories/{a['id']}", json={"depends_on": [a["id"]]})
    assert res.status_code == 400 and "itself" in res.json()["detail"]
    res = client.patch(f"/api/v1/stories/{a['id']}", json={"depends_on": [b["id"]]})
    assert res.status_code == 400 and "cycle" in res.json()["detail"]
    c = create_story(client, depends_on=[b["id"]])
    res = client.patch(f"/api/v1/stories/{a['id']}", json={"depends_on": [c["id"]]})
    assert res.status_code == 400 and "cycle" in res.json()["detail"]


def test_scheduler_skips_blocked_stories(client):
    a = create_story(client, title="unblock first")
    b = create_story(client, title="blocked one", depends_on=[a["id"]])
    client.post(f"/api/v1/stories/{a['id']}/queue")
    client.post(f"/api/v1/stories/{b['id']}/queue")

    # a runs to human_review; b must still be queued (dependency unfinished)
    wait_for_status(client, a["id"], "human_review")
    assert client.get(f"/api/v1/stories/{b['id']}").json()["status"] == "queued"

    # finish a (write tools off -> approve completes) -> b becomes eligible
    client.post(f"/api/v1/stories/{a['id']}/approve", json={"by": "t"})
    b_done = wait_for_status(client, b["id"], "human_review")
    assert b_done["status"] == "human_review"


def test_blocking_list_shows_only_unfinished(client):
    a = create_story(client)
    b = create_story(client, depends_on=[a["id"]])
    client.patch(f"/api/v1/stories/{a['id']}", json={"status": "backlog"})  # no-op guard
    detail = client.get(f"/api/v1/stories/{b['id']}").json()
    assert len(detail["blocking"]) == 1


def test_failed_dependency_cascades_to_dependents(client, monkeypatch, tmp_path):
    # force an immediate provider failure for the prerequisite
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers import GatewayError

    class BoomGateway:
        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            raise GatewayError("[echo] kaput")
            yield  # pragma: no cover

    # per-test DB (tmp_path is worker-unique under xdist; a fixed /tmp path
    # would collide between workers and between parallel stories)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "cascade.db"))
    with TestClient(create_app(gateway=BoomGateway())) as c:
        c.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
        a = c.post("/api/v1/stories", json={"title": "root"}).json()
        b = c.post("/api/v1/stories", json={"title": "child", "depends_on": [a["id"]]}).json()
        d = c.post("/api/v1/stories", json={"title": "grandchild", "depends_on": [b["id"]]}).json()
        running = c.post("/api/v1/stories", json={"title": "unrelated"}).json()

        # queue the dependent FIRST: the cascade fires once, when a fails (a
        # tick after a is queued). If b were queued after that pass — possible
        # when a xdist-loaded worker stretches the gap between the two POSTs
        # past the 50ms poll — the cascade would never see b queued and it
        # would sit queued forever.
        c.post(f"/api/v1/stories/{b['id']}/queue")
        c.post(f"/api/v1/stories/{a['id']}/queue")

        # the cascade to dependents happens right after the prerequisite's
        # failure lands — poll for it instead of asserting immediately (a
        # slow tick could still be mid-cascade)
        child = wait_for_status(c, b["id"], "failed")
        grandchild = wait_for_status(c, d["id"], "failed")
        assert "dependency failed: root" in child["error"]
        assert grandchild["status"] == "failed"  # transitive cascade
        assert c.get(f"/api/v1/stories/{running['id']}").json()["status"] == "backlog"


def test_retry_after_infra_failure_reopens_cascade(client, monkeypatch, tmp_path):
    """An infrastructure failure cascades like any other; the retry restores
    the dependents exactly as a cancel-and-retry does (same _fail path)."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers import GatewayError

    class InfraBoomGateway:
        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            # "authenticationerror"/"api key" are INFRA_MARKERS: the story
            # fails with the "infrastructure: " prefix, not a requeue
            raise GatewayError("[echo] AuthenticationError: invalid api key")
            yield  # pragma: no cover

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "infra-cascade.db"))
    with TestClient(create_app(gateway=InfraBoomGateway())) as c:
        c.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
        root = c.post("/api/v1/stories", json={"title": "root"}).json()
        b = c.post(
            "/api/v1/stories", json={"title": "child", "depends_on": [root["id"]]}
        ).json()
        d = c.post(
            "/api/v1/stories", json={"title": "grandchild", "depends_on": [b["id"]]}
        ).json()
        c.post(f"/api/v1/stories/{b['id']}/queue")
        c.post(f"/api/v1/stories/{root['id']}/queue")

        root_final = wait_for_status(c, root["id"], "failed")
        assert root_final["error"].startswith("infrastructure: ")
        child = wait_for_status(c, b["id"], "failed")
        grandchild = wait_for_status(c, d["id"], "failed")
        assert child["error"] == "dependency failed: root"
        assert grandchild["error"] == "dependency failed: child"

        # scheduler HARD-off so the assertions below cannot race a fresh run
        # of the retried root (which would fail again on the same gateway)
        c.portal.call(c.app.state.story_runner.stop)

        out = c.post(f"/api/v1/stories/{root['id']}/retry", json={"stage": "queued"}).json()
        assert out["status"] == "queued" and out["error"] is None

        assert c.get(f"/api/v1/stories/{b['id']}").json()["status"] == "queued"
        restored = c.get(f"/api/v1/stories/{d['id']}").json()
        assert restored["status"] == "backlog" and restored["error"] is None
        notes = [e["data"] for e in restored["events"] if e["kind"] == "note"]
        assert any("re-opened: child was retried" in t for t in notes), notes
