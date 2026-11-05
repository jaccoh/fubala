"""Project brief: versioned Markdown per project (REST, helpers, spec prompt)."""

import time

from test_execution import init_repo, phase_of
from test_stories import create_story, wait_for_status

from app.brief import (
    BRIEF_TEMPLATE,
    approx_tokens,
    extract_guard_sections,
    spec_prompt_block,
)
from app.db import session_scope
from app.models import Epic, Project, ProjectBriefVersion

OWNER_SENTINEL = "OWNER-RULE-SENTINEL-7f3a"
THREADS_SENTINEL = "OPEN-THREADS-SENTINEL-91bc"
EPIC_SENTINEL = "EPIC-DESCRIPTION-SENTINEL-c2d4"

BRIEF = f"""# Demo

## Purpose and owner goals
goal: demo

## owner RULES
- {OWNER_SENTINEL}

### Sub rule
- keep this child

## Non-goals
- no mobile app

```
## Open threads and next steps
fenced: still non-goals
```

## Live state
status: fine

## Open threads and next steps
- {THREADS_SENTINEL}

## IDs and links
repo: x
"""


def make_project(client, root, **extra) -> dict:
    res = client.post("/api/v1/projects", json={"name": "demo", "root": str(root), **extra})
    assert res.status_code == 201, res.text
    return res.json()


def put_brief(client, pid, body, base, author="tester"):
    return client.put(
        f"/api/v1/projects/{pid}/brief",
        json={"body": body, "base_version": base, "author": author},
    )


def test_put_then_get_round_trip(client, tmp_path):
    pid = make_project(client, tmp_path)["id"]
    res = put_brief(client, pid, "## Owner rules\n- be kind\n", 0)
    assert res.status_code == 200, res.text
    assert res.json()["version"] == 1
    got = client.get(f"/api/v1/projects/{pid}/brief").json()
    assert got["body"] == "## Owner rules\n- be kind\n"
    assert got["version"] == 1
    assert got["author"] == "tester"
    assert "template" not in got


def test_stale_put_is_409_and_adds_no_row(client, tmp_path):
    pid = make_project(client, tmp_path)["id"]
    assert put_brief(client, pid, "first", 0).status_code == 200
    res = put_brief(client, pid, "stale", 0, author="other")
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["message"]
    assert detail["current"]["version"] == 1
    assert detail["current"]["body"] == "first"
    versions = client.get(f"/api/v1/projects/{pid}/brief/versions").json()
    assert [v["version"] for v in versions] == [1]


def test_versions_newest_first_and_restore_appends(client, tmp_path):
    pid = make_project(client, tmp_path)["id"]
    put_brief(client, pid, "one", 0)
    put_brief(client, pid, "two two", 1, author="b")
    versions = client.get(f"/api/v1/projects/{pid}/brief/versions").json()
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["author"] == "b"
    assert versions[0]["tokens"] == approx_tokens("two two")
    assert all("body" not in v for v in versions)

    old = client.get(f"/api/v1/projects/{pid}/brief?version=1").json()
    assert old["body"] == "one"
    restored = put_brief(client, pid, old["body"], 2).json()
    assert restored["version"] == 3
    assert client.get(f"/api/v1/projects/{pid}/brief").json()["body"] == "one"
    assert client.get(f"/api/v1/projects/{pid}/brief?version=9").status_code == 404


def test_empty_brief_has_budget_and_template(client, tmp_path):
    project = make_project(client, tmp_path)
    assert project["brief_token_budget"] == 8000
    assert project["brief_in_spec_prompt"] is False
    got = client.get(f"/api/v1/projects/{project['id']}/brief").json()
    assert got["version"] == 0 and got["body"] == ""
    assert got["tokens"] == 0 and got["budget"] == 8000 and got["over_budget"] is False
    assert got["template"] == BRIEF_TEMPLATE

    client.patch(f"/api/v1/projects/{project['id']}", json={"brief_token_budget": 2})
    put_brief(client, project["id"], "x" * 20, 0)
    got = client.get(f"/api/v1/projects/{project['id']}/brief").json()
    assert got["tokens"] == 5 and got["budget"] == 2 and got["over_budget"] is True
    assert (
        client.patch(
            f"/api/v1/projects/{project['id']}", json={"brief_token_budget": 0}
        ).status_code
        == 422
    )


def test_delete_project_deletes_brief_versions(client, tmp_path):
    pid = make_project(client, tmp_path)["id"]
    put_brief(client, pid, "a", 0)
    put_brief(client, pid, "b", 1)
    assert client.delete(f"/api/v1/projects/{pid}").status_code == 204
    with session_scope(client.app.state.engine) as db:
        from sqlmodel import select

        assert db.exec(select(ProjectBriefVersion)).all() == []


def test_template_has_eight_headings():
    headings = [ln[3:] for ln in BRIEF_TEMPLATE.splitlines() if ln.startswith("## ")]
    assert headings == [
        "Purpose and owner goals",
        "Owner rules",
        "Non-goals",
        "Live state",
        "Architecture map",
        "How we work",
        "Open threads and next steps",
        "IDs and links",
    ]


def test_extract_guard_sections_keeps_only_the_two():
    sections = extract_guard_sections(BRIEF)
    assert list(sections) == ["owner RULES", "Non-goals"]
    assert OWNER_SENTINEL in sections["owner RULES"]
    assert "### Sub rule" in sections["owner RULES"]
    assert "keep this child" in sections["owner RULES"]
    assert "no mobile app" in sections["Non-goals"]
    assert "fenced: still non-goals" in sections["Non-goals"]
    joined = "\n".join(sections.values())
    for dropped in ("goal: demo", "status: fine", THREADS_SENTINEL, "repo: x"):
        assert dropped not in joined


def _seed(client, tmp_path, body, flag, epic_description=""):
    pid = make_project(client, tmp_path, brief_in_spec_prompt=flag)["id"]
    put_brief(client, pid, body, 0)
    with session_scope(client.app.state.engine) as db:
        epic = Epic(title="e", description=epic_description)
        db.add(epic)
        db.flush()
        epic_id = epic.id
    return pid, epic_id


def test_spec_prompt_block_off_and_empty(client, tmp_path):
    pid, epic_id = _seed(client, tmp_path, BRIEF, False)
    with session_scope(client.app.state.engine) as db:
        assert spec_prompt_block(db, pid, epic_id) == ("", None)

    empty = "## Owner rules\n\n## Non-goals\n   \n## Live state\nx\n"
    client.put(
        f"/api/v1/projects/{pid}/brief",
        json={"body": empty, "base_version": 1, "author": "t"},
    )
    client.patch(f"/api/v1/projects/{pid}", json={"brief_in_spec_prompt": True})
    with session_scope(client.app.state.engine) as db:
        assert db.get(Project, pid).brief_in_spec_prompt is True
        assert spec_prompt_block(db, pid, epic_id) == ("", None)


def test_spec_prompt_block_on(client, tmp_path):
    pid, epic_id = _seed(client, tmp_path, BRIEF, True, EPIC_SENTINEL)
    with session_scope(client.app.state.engine) as db:
        text, version = spec_prompt_block(db, pid, epic_id)
    assert version == 1
    assert text.startswith("## Project brief v1 (owner rules, non-goals)")
    assert OWNER_SENTINEL in text and EPIC_SENTINEL in text
    assert THREADS_SENTINEL not in text and "status: fine" not in text
    assert "brief v1" in text.splitlines()[-1]


def _wait_settled(client, sid, timeout=30.0) -> dict:
    deadline = time.time() + timeout
    story = {}
    while time.time() < deadline:
        story = client.get(f"/api/v1/stories/{sid}").json()
        if story["status"] in ("human_review", "failed", "done"):
            return story
        time.sleep(0.05)
    return story


def test_spec_writer_gets_guard_sections_and_coder_gets_none(client, gateway, tmp_path):
    repo = init_repo(tmp_path)
    # write tools on: a spec approval then runs the coder (off = spec only)
    # (_execute_story also insists on the app-level project_root)
    client.patch(
        "/api/v1/settings", json={"write_tools_enabled": True, "project_root": repo}
    )
    pid, epic_id = _seed(client, repo, BRIEF, True, EPIC_SENTINEL)
    story = create_story(client, project_id=pid, epic_id=epic_id)
    client.post(f"/api/v1/stories/{story['id']}/queue")
    parked = wait_for_status(client, story["id"], "human_review")
    assert "brief v1" in parked["spec"]

    spec_calls = [c for c in gateway.calls if phase_of(c["messages"]) == "=== SPEC REQUEST ==="]
    assert spec_calls
    system = spec_calls[0]["messages"][0]["content"]
    assert OWNER_SENTINEL in system and EPIC_SENTINEL in system
    assert THREADS_SENTINEL not in system

    client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
    _wait_settled(client, story["id"])
    coder_calls = [
        c for c in gateway.calls if phase_of(c["messages"]) == "=== CODE REQUEST ==="
    ]
    assert coder_calls
    for call in coder_calls:
        text = repr(call["messages"])
        assert OWNER_SENTINEL not in text and THREADS_SENTINEL not in text
