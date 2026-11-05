"""The pod serves MCP over streamable HTTP at /mcp behind the Bearer wall."""

import json
import logging

import pytest
from fastapi.testclient import TestClient
from test_stories import create_story, wait_for_status

from app.brief import approx_tokens
from app.main import create_app
from app.mcp_tools import INSTRUCTIONS_PATH, INSTRUCTIONS_TOKEN_BUDGET, ROUTE_TOOLS

HEADERS = {"Accept": "application/json, text/event-stream"}


def rpc(client, method, params=None, bearer=None):
    headers = dict(HEADERS)
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


def body_of(res) -> dict:
    """The JSON-RPC message, whether sent as JSON or as one SSE event."""
    if res.headers["content-type"].startswith("application/json"):
        return res.json()
    data = [ln[5:].strip() for ln in res.text.splitlines() if ln.startswith("data:")]
    return json.loads(data[-1])


def call(client, name, arguments, bearer=None):
    res = rpc(client, "tools/call", {"name": name, "arguments": arguments}, bearer)
    assert res.status_code == 200, res.text
    result = body_of(res)["result"]
    assert not result.get("isError"), result
    return result["content"][0]["text"]


def call_raw(client, name, arguments, bearer=None):
    return json.loads(call(client, name, {**arguments, "raw": True}, bearer))


@pytest.fixture
def app_client(tmp_path, monkeypatch, gateway):
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "mcp.db"))
    with TestClient(create_app(gateway=gateway)) as c:
        c.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
        yield c


def parked_story(client) -> dict:
    """A story waiting at the spec gate, with the runner stopped so it stays."""
    story = create_story(client)
    client.post(f"/api/v1/stories/{story['id']}/queue")
    wait_for_status(client, story["id"], "human_review")
    client.portal.call(client.app.state.story_runner.stop)
    return story


def test_tools_list_returns_every_routed_tool(app_client):
    res = rpc(app_client, "tools/list")
    assert res.status_code == 200
    names = {t["name"] for t in body_of(res)["result"]["tools"]}
    assert set(ROUTE_TOOLS.values()) <= names
    assert "story_action" in names


def test_mcp_path_has_no_redirect(app_client):
    res = rpc(app_client, "tools/list")
    assert res.history == [] and res.status_code == 200


def test_bearer_required_when_auth_is_on(app_client):
    token = app_client.post("/api/v1/auth/token").json()["token"]
    assert app_client.patch("/api/v1/settings", json={"api_auth_enabled": True}).status_code == 200

    assert rpc(app_client, "tools/list").status_code == 401
    assert rpc(app_client, "tools/list", bearer="fubal_wrong").status_code == 401
    assert rpc(app_client, "tools/list", bearer=token).status_code == 200


def test_create_roundtrip_matches_rest(app_client):
    created = call_raw(app_client, "create_story", {"title": "via mcp", "description": "d"})
    rest = app_client.get(f"/api/v1/stories/{created['id']}").json()
    for key in ("id", "title", "description", "status", "model", "kind"):
        assert created[key] == rest[key]


def test_note_roundtrip_matches_rest(app_client):
    story = create_story(app_client)
    args = {"story_id": story["id"], "body": "hi", "author": "t"}
    note = call_raw(app_client, "add_story_note", args)
    notes = app_client.get(f"/api/v1/stories/{story['id']}").json()["notes"]
    assert [n for n in notes if n["id"] == note["id"]] == [note]


def test_approve_roundtrip_matches_rest(app_client):
    story = parked_story(app_client)
    approved = call_raw(app_client, "approve_story", {"story_id": story["id"]})
    rest = app_client.get(f"/api/v1/stories/{story['id']}").json()
    assert approved["status"] == rest["status"] != "human_review"
    assert approved["id"] == rest["id"]


def test_reject_roundtrip_matches_rest(app_client):
    story = parked_story(app_client)
    rejected = call_raw(app_client, "reject_story", {"story_id": story["id"], "reason": "no"})
    rest = app_client.get(f"/api/v1/stories/{story['id']}").json()
    assert rejected["status"] == rest["status"] == "queued"


def test_settings_patch_roundtrip_matches_rest(app_client):
    patched = call_raw(
        app_client, "patch_settings", {"values": {"max_parallel_stories": 3}, "by": "tester"}
    )
    assert patched["max_parallel_stories"] == 3
    assert app_client.get("/api/v1/settings").json() == patched


def test_destructive_tools_refuse_empty_by(app_client):
    story = create_story(app_client)
    for name, args in (
        ("delete_story", {"story_id": story["id"]}),
        ("discard_story", {"story_id": story["id"]}),
        ("delete_project", {"project_id": "x"}),
        ("delete_epic", {"epic_id": "x"}),
        ("delete_provider", {"provider_id": "x"}),
        ("delete_provider_model", {"provider_id": "x", "model_row_id": 1}),
        ("delete_chat_session", {"session_id": "x"}),
        ("patch_settings", {"values": {"max_parallel_stories": 2}}),
    ):
        assert "'by' is required" in call(app_client, name, {**args, "by": "  "}), name
    assert app_client.get(f"/api/v1/stories/{story['id']}").status_code == 200
    assert app_client.get("/api/v1/auth/token").json()["hint"] is None


def test_destructive_call_logs_actor(app_client, caplog):
    story = create_story(app_client)
    with caplog.at_level(logging.INFO, logger="app.mcp_tools"):
        call(app_client, "delete_story", {"story_id": story["id"], "by": "owner"})
    assert f"mcp destructive tool=delete_story by=owner target={story['id']}" in caplog.text


def test_token_routes_have_no_tools(app_client):
    names = {t["name"] for t in body_of(rpc(app_client, "tools/list"))["result"]["tools"]}
    assert not {"rotate_api_token", "clear_api_token"} & names


def test_read_tools_never_leak_the_api_token(app_client, caplog):
    token = app_client.post("/api/v1/auth/token").json()["token"]
    story = create_story(app_client)
    session = call_raw(app_client, "create_chat_session", {})
    calls = [
        ("list_stories", {}),
        ("get_story", {"story_id": story["id"]}),
        ("list_projects", {}),
        ("list_epics", {}),
        ("get_settings", {}),
        ("get_prompt_defaults", {}),
        ("list_providers", {}),
        ("list_provider_presets", {}),
        ("list_provider_limits", {}),
        ("list_models", {}),
        ("list_chat_sessions", {}),
        ("get_chat_session", {"session_id": session["id"]}),
        ("list_pending_permissions", {}),
        ("get_token_status", {}),
    ]
    with caplog.at_level(logging.DEBUG):
        for name, args in calls:
            for raw in (False, True):
                out = call(app_client, name, {**args, "raw": raw})
                assert token not in out, name
    assert token not in caplog.text
    assert all(token not in r.getMessage() for r in caplog.records)


def test_204_raw_and_delete(app_client):
    story = create_story(app_client)
    args = {"story_id": story["id"], "by": "t"}
    assert call_raw(app_client, "delete_story", args) == {"status": 204}
    assert app_client.get(f"/api/v1/stories/{story['id']}").status_code == 404


def test_send_chat_message_returns_final_text(app_client):
    session = call_raw(app_client, "create_chat_session", {})
    args = {"session_id": session["id"], "content": "hi"}
    assert call(app_client, "send_chat_message", args) == "Hello there!"


def test_provider_summary_never_shows_the_key(app_client):
    body = {"preset": "zai", "id": "zai-extra", "api_key": "secret-key-1234"}
    app_client.post("/api/v1/providers", json=body)
    text = call(app_client, "list_providers", {})
    assert "secret-key-1234" not in text and "…1234" in text


# ---- server instructions -----------------------------------------------------

SEPARATOR = "\n\n## Project: How we work\n\n"


def _base() -> str:
    return INSTRUCTIONS_PATH.read_text(encoding="utf-8")


def _project_with_brief(client, tmp_path, name, body=None) -> dict:
    project = client.post("/api/v1/projects", json={"name": name, "root": str(tmp_path)}).json()
    if body is not None:
        saved = client.put(
            f"/api/v1/projects/{project['id']}/brief",
            json={"body": body, "base_version": 0, "author": "t"},
        )
        assert saved.status_code == 200, saved.text
    return project


def test_initialize_carries_the_instructions(app_client):
    res = rpc(
        app_client,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    )
    assert body_of(res)["result"]["instructions"] == _base()


def test_instructions_tool_without_project_covers_the_six_rules(app_client):
    text = call(app_client, "get_fubala_instructions", {})
    assert text == _base()
    assert approx_tokens(text) <= INSTRUCTIONS_TOKEN_BUDGET
    for phrase in (
        "First call `get_workspace_brief`",
        "`create_story`",
        "acceptance criteria",
        "`approve_story`",
        "`get_story_diff`",
        "`wait_for_story`",
        "`base_version`",
        "Never print, store or send a token",
    ):
        assert phrase in text


def test_instructions_tool_appends_how_we_work(app_client, tmp_path):
    project = _project_with_brief(
        app_client,
        tmp_path,
        "alpha",
        "## Owner rules\n- a\n\n## How we work\nShip small.\n\n## Next\nx\n",
    )
    text = call(app_client, "get_fubala_instructions", {"project": project["id"]})
    assert text == _base() + SEPARATOR + "Ship small."


def test_instructions_tool_without_section_or_brief_is_base_only(app_client, tmp_path):
    with_brief = _project_with_brief(app_client, tmp_path, "alpha", "## Owner rules\n- a\n")
    assert call(app_client, "get_fubala_instructions", {"project": with_brief["id"]}) == _base()
    no_brief = _project_with_brief(app_client, tmp_path, "bravo")
    assert call(app_client, "get_fubala_instructions", {"project": no_brief["id"]}) == _base()


def test_instructions_tool_truncates_an_oversized_section(app_client, tmp_path):
    project = _project_with_brief(
        app_client, tmp_path, "alpha", "## How we work\n" + "word " * 5000 + "\n"
    )
    text = call(app_client, "get_fubala_instructions", {"project": project["id"]})
    assert text.startswith(_base() + SEPARATOR)
    assert text.endswith("[truncated: read get_project_brief]")
    assert approx_tokens(text) <= INSTRUCTIONS_TOKEN_BUDGET


def test_instructions_cover_the_coordinator_minimum():
    text = _base()
    for phrase in (
        # 1. decisions go into the spec or the reject reason
        "INTO the spec",
        "INTO the code-gate reject reason",
        "Approval notes",
        # 2. story kinds
        "info: done when the spec is approved",
        "prep: the deliverable is child stories",
        "`depends_on`",
        # 3. gate actions
        "approve:",
        "reject:",
        "retry (`retry_story`): only for failed, backlog or queued",
        "respec",
        "supersede: only for failed or human_review",
        "cancel:",
        "queue / unqueue",
        "`override_verify`",
        "`justification`",
        # 4. creation rules
        "Never pass `skill`",
        "project default",
        "required when there are two or more",
        # 5. landing
        "merge train",
        "per-PR land job",
        "parks the story at human_review",
        "`get_project_train`",
        "`retry_land`",
        "`unland_story`",
        "`pause_project_train`",
        "`resume_project_train`",
        "`rerun_train_batch`",
        "`bounce_train_member`",
        "`land_train_batch_now`",
        "`force_merge_train_batch`",
        "Every train operation takes `by`",
        # 6. authority
        "Approve or reject only when the operator gave you that authority",
    ):
        assert phrase in text, phrase
    assert "explicitly told you to" not in text
