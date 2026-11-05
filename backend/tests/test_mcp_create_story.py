"""create_story's `project` argument: id / id-prefix / name resolution and
the omitted-project policy.

Same mechanics as test_mcp_gate.py: monkeypatch app.mcp._get / _post, then
asyncio.run the tool. _resolve_project and create_story share the module-level
_get, so the scripted _get dispatches on path (/projects vs /stories/<id>).
"""

import asyncio

from app import mcp_tools as mcp

# ---- fixtures -----------------------------------------------------------


def _project(pid: str, name: str) -> dict:
    """Minimal _project_out-shaped dict (only the keys _resolve_project reads)."""
    return {"id": pid, "name": name}


def _story(**overrides) -> dict:
    """Minimal StoryDetailOut-shaped dict covering _story_summary's reads."""
    story = {
        "id": "story-1",
        "title": "Project me",
        "status": "backlog",
        "depends_on": [],
        "spec_version": 1,
        "model": "zai/glm-5.3",
        "skill": None,
        "execution": "internal",
        "execution_override": None,
        "claimed_by": None,
        "ai_verdict": None,
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
    story.update(overrides)
    return story


def _setup(monkeypatch, projects: list[dict], *, get_error: Exception | None = None):
    """Script _get (dispatching /projects vs story paths) and _post; return
    the captured call log."""
    calls: dict = {"gets": [], "posted": None}

    async def scripted_get(path):
        calls["gets"].append(path)
        if get_error is not None:
            raise get_error
        if path == "/projects":
            return projects
        return _story()

    async def scripted_post(path, body=None):
        calls["posted"] = {"path": path, "body": body}
        return _story()

    monkeypatch.setattr(mcp, "_get", scripted_get)
    monkeypatch.setattr(mcp, "_post", scripted_post)
    return calls


# ---- happy paths --------------------------------------------------------


def test_full_id_resolves_and_lands_in_post_body(monkeypatch):
    calls = _setup(monkeypatch, [_project("alpha-1111-2222", "Alpha")])
    out = asyncio.run(mcp.create_story("t", project="alpha-1111-2222"))
    assert calls["posted"]["body"]["project_id"] == "alpha-1111-2222"
    assert out.startswith("created in backlog")


def test_id_prefix_resolves(monkeypatch):
    calls = _setup(monkeypatch, [_project("alpha-1111-2222", "Alpha")])
    asyncio.run(mcp.create_story("t", project="alpha-11"))
    assert calls["posted"]["body"]["project_id"] == "alpha-1111-2222"


def test_name_as_list_projects_shows_it_resolves(monkeypatch):
    calls = _setup(monkeypatch, [_project("alpha-1111-2222", "Alpha")])
    asyncio.run(mcp.create_story("t", project="Alpha"))
    assert calls["posted"]["body"]["project_id"] == "alpha-1111-2222"


def test_name_match_is_case_insensitive_when_unique(monkeypatch):
    calls = _setup(monkeypatch, [_project("alpha-1111-2222", "Alpha")])
    asyncio.run(mcp.create_story("t", project="aLpHa"))
    assert calls["posted"]["body"]["project_id"] == "alpha-1111-2222"


def test_short_name_still_resolves_despite_prefix_rule(monkeypatch):
    """A 5-char project name must resolve as a name — the >=8 prefix rule
    applies to ids, not to names."""
    calls = _setup(monkeypatch, [_project("proj-0001-aaaa", "Acme")])
    asyncio.run(mcp.create_story("t", project="Acme"))
    assert calls["posted"]["body"]["project_id"] == "proj-0001-aaaa"


def test_exact_id_wins_over_same_prefix(monkeypatch):
    projects = [
        _project("proj-0001-aaaa", "One"),
        _project("proj-0001-bbbb", "Two"),
    ]
    calls = _setup(monkeypatch, projects)
    asyncio.run(mcp.create_story("t", project="proj-0001-bbbb"))
    assert calls["posted"]["body"]["project_id"] == "proj-0001-bbbb"


def test_projects_fetched_exactly_once(monkeypatch):
    calls = _setup(monkeypatch, [_project("alpha-1111-2222", "Alpha")])
    asyncio.run(mcp.create_story("t", project="Alpha"))
    assert calls["gets"].count("/projects") == 1


# ---- omitted-project policy --------------------------------------------


def test_omitted_with_no_projects_omits_project_id(monkeypatch):
    calls = _setup(monkeypatch, [])
    asyncio.run(mcp.create_story("t"))
    assert "project_id" not in calls["posted"]["body"]


def test_omitted_with_single_project_uses_it(monkeypatch):
    calls = _setup(monkeypatch, [_project("only-2222-3333", "Only")])
    out = asyncio.run(mcp.create_story("t"))
    assert calls["posted"]["body"]["project_id"] == "only-2222-3333"
    assert out.startswith("created in backlog")


def test_omitted_with_two_projects_refuses_without_posting(monkeypatch):
    calls = _setup(
        monkeypatch,
        [_project("alpha-1111-2222", "Alpha"), _project("beta-3333-4444", "Beta")],
    )
    out = asyncio.run(mcp.create_story("t"))
    assert calls["posted"] is None
    assert out.startswith("failed to create story:")
    assert "Alpha" in out
    assert "Beta" in out
    assert "pass project" in out


# ---- refusals ----------------------------------------------------------


def test_unknown_project_refuses_without_posting(monkeypatch):
    calls = _setup(monkeypatch, [_project("alpha-1111-2222", "Alpha")])
    out = asyncio.run(mcp.create_story("t", project="nope"))
    assert calls["posted"] is None
    assert out.startswith("failed to create story:")
    assert "nope" in out
    assert "Alpha" in out
    assert "list_projects" in out


def test_ambiguous_prefix_lists_candidates(monkeypatch):
    projects = [
        _project("proj-0001-aaaa", "One"),
        _project("proj-0001-bbbb", "Two"),
    ]
    calls = _setup(monkeypatch, projects)
    out = asyncio.run(mcp.create_story("t", project="proj-0001"))
    assert calls["posted"] is None
    assert "ambiguous" in out
    assert "One" in out
    assert "Two" in out


def test_ambiguous_name_refuses(monkeypatch):
    projects = [
        _project("alpha-1111-2222", "Duplicate"),
        _project("beta-3333-4444", "Duplicate"),
    ]
    calls = _setup(monkeypatch, projects)
    out = asyncio.run(mcp.create_story("t", project="Duplicate"))
    assert calls["posted"] is None
    assert "ambiguous" in out


def test_prefix_shorter_than_list_projects_shows_errors(monkeypatch):
    # "alpha" is 5 chars and is NOT the project's name, so it can only be an
    # id prefix — and too short a one for what list_projects displays.
    calls = _setup(monkeypatch, [_project("alpha-1111-2222", "Different")])
    out = asyncio.run(mcp.create_story("t", project="alpha"))
    assert calls["posted"] is None
    assert out.startswith("failed to create story:")
    assert "too short" in out


# ---- transport failure -------------------------------------------------


def test_projects_endpoint_failure_surfaces_prefixed(monkeypatch):
    calls = _setup(monkeypatch, [], get_error=RuntimeError("401: auth required"))
    out = asyncio.run(mcp.create_story("t", project="Alpha"))
    assert calls["posted"] is None
    assert out == "failed to create story: 401: auth required"


# ---- docs --------------------------------------------------------------


def test_docstring_documents_project_and_refusal():
    doc = mcp.create_story.__doc__
    assert "project" in doc
    assert "list_projects" in doc
    assert "refused" in doc
