"""Chat session CRUD lifecycle."""


def test_create_get_list_delete_lifecycle(client):
    res = client.post("/api/v1/chat/sessions", json={})
    assert res.status_code == 201
    session = res.json()
    assert session["model"] == "echo/dev"
    assert session["title"] == "New chat"
    assert session["id"]

    # detail starts empty
    detail = client.get(f"/api/v1/chat/sessions/{session['id']}").json()
    assert detail["messages"] == []

    # second session shows up in the list
    other = client.post("/api/v1/chat/sessions", json={"title": "Second"}).json()
    ids = [s["id"] for s in client.get("/api/v1/chat/sessions").json()]
    assert {session["id"], other["id"]} <= set(ids)

    # delete removes it (and cascades its messages later)
    assert client.delete(f"/api/v1/chat/sessions/{other['id']}").status_code == 204
    assert client.get(f"/api/v1/chat/sessions/{other['id']}").status_code == 404


def test_create_with_model_and_title(client):
    res = client.post(
        "/api/v1/chat/sessions",
        json={"title": "GLM chat", "model": "zai/glm-4.7"},
    )
    assert res.status_code == 201
    assert res.json()["model"] == "zai/glm-4.7"


def test_create_with_unknown_model_rejected(client):
    res = client.post("/api/v1/chat/sessions", json={"model": "nope/whatever"})
    assert res.status_code == 400


def test_patch_updates_title_and_model(client):
    sid = client.post("/api/v1/chat/sessions", json={}).json()["id"]

    res = client.patch(
        f"/api/v1/chat/sessions/{sid}",
        json={"title": "Renamed", "model": "openrouter/z-ai/glm-4.7"},
    )
    assert res.status_code == 200
    assert res.json()["title"] == "Renamed"
    assert res.json()["model"] == "openrouter/z-ai/glm-4.7"

    assert client.patch(f"/api/v1/chat/sessions/{sid}", json={"model": "x/y"}).status_code == 400
    assert client.patch("/api/v1/chat/sessions/missing", json={"title": "t"}).status_code == 404
