"""MCP tools for the project brief (over the HTTP /mcp endpoint)."""

from test_mcp_http import app_client, call, call_raw  # noqa: F401


def _project(client, name, root, enabled=True) -> dict:
    project = client.post(
        "/api/v1/projects", json={"name": name, "root": str(root)}
    ).json()
    if not enabled:
        client.patch(f"/api/v1/projects/{project['id']}", json={"enabled": False})
    return project


def test_brief_tools_round_trip(app_client, tmp_path):  # noqa: F811
    project = _project(app_client, "alpha", tmp_path)
    empty = call_raw(app_client, "get_project_brief", {"project": "alpha"})
    assert empty["version"] == 0 and "template" in empty

    saved = call_raw(
        app_client,
        "update_project_brief",
        {"project": "alpha", "body": "## Owner rules\n- one\n", "base_version": 0, "by": "mcp"},
    )
    assert saved["version"] == 1
    text = call(app_client, "get_project_brief", {"project": project["id"]})
    assert "brief v1 by mcp" in text and "- one" in text

    call(
        app_client,
        "update_project_brief",
        {"project": "alpha", "body": "two", "base_version": 1, "by": "mcp"},
    )
    versions = call_raw(app_client, "list_project_brief_versions", {"project": "alpha"})
    assert [v["version"] for v in versions] == [2, 1]
    old = call_raw(app_client, "get_project_brief", {"project": "alpha", "version": 1})
    assert old["body"] == "## Owner rules\n- one\n"


def test_update_project_brief_refuses_stale_base(app_client, tmp_path):  # noqa: F811
    _project(app_client, "alpha", tmp_path)
    call(
        app_client,
        "update_project_brief",
        {"project": "alpha", "body": "current body", "base_version": 0, "by": "a"},
    )
    text = call(
        app_client,
        "update_project_brief",
        {"project": "alpha", "body": "late", "base_version": 0, "by": "b"},
    )
    assert text.startswith("refused: stale base_version")
    assert "v1 by a" in text and "current body" in text
    raw = call_raw(
        app_client,
        "update_project_brief",
        {"project": "alpha", "body": "late", "base_version": 0, "by": "b"},
    )
    assert raw["refused"] == "stale base_version"
    assert raw["current"]["version"] == 1


def test_workspace_brief_lists_enabled_projects_only(app_client, tmp_path):  # noqa: F811
    on = _project(app_client, "alpha", tmp_path / "a")
    _project(app_client, "bravo", tmp_path / "b")
    off = _project(app_client, "charlie", tmp_path / "c", enabled=False)
    call(
        app_client,
        "update_project_brief",
        {"project": "alpha", "body": "ALPHA BRIEF", "base_version": 0, "by": "t"},
    )
    text = call(app_client, "get_workspace_brief", {})
    assert f"# alpha ({on['id']})" in text
    assert "version 1 / tokens 3 / budget 8000" in text
    assert "ALPHA BRIEF" in text
    assert "# bravo (" in text and "(no brief)" in text
    assert "charlie" not in text and off["id"] not in text
