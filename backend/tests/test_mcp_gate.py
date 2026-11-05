"""MCP gate tools: get_story_diff, verify/code-review rendering in get_story,
and story_action approve payload forwarding.

A gatekeeper using MCP must be able to review the worktree diff and the
verify output and then approve (with verify override) without falling back
to raw REST.

Uses the same direct-call mechanics as test_mcp_wait.py: monkeypatch
app.mcp helpers (_get / _get_text / _post), then asyncio.run the tool.
"""

import asyncio

from app import mcp_tools as mcp

# ---- fixtures -----------------------------------------------------------


def _story(**overrides) -> dict:
    """Minimal StoryDetailOut-shaped dict covering _story_summary's reads."""
    story = {
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
    story.update(overrides)
    return story


def _diff() -> str:
    """Unified diff with exactly known per-file +/- counts (excluding the
    +++/--- header lines)."""
    return "\n".join(
        [
            "diff --git a/apples.py b/apples.py",
            "index 1111111..2222222 100644",
            "--- a/apples.py",
            "+++ b/apples.py",
            "@@ -1,3 +1,3 @@",
            " context",
            "+apples added",
            "-apples removed",
            "diff --git a/cherries.py b/cherries.py",
            "index 3333333..4444444 100644",
            "--- a/cherries.py",
            "+++ b/cherries.py",
            "@@ -1,2 +1,5 @@",
            "+cherries one",
            "+cherries two",
            "+cherries three",
            " context stays",
            "diff --git a/dates.py b/dates.py",
            "index 5555555..6666666 100644",
            "--- a/dates.py",
            "+++ b/dates.py",
            "@@ -1,3 +1,1 @@",
            "-dates one",
            "-dates two",
        ]
    )


def _setup_get(monkeypatch, story: dict):
    calls = {"paths": []}

    async def scripted_get(path):
        calls["paths"].append(path)
        return story

    monkeypatch.setattr(mcp, "_get", scripted_get)
    return calls


def _setup_get_text(monkeypatch, text: str, *, status: int = 200):
    calls = {"paths": []}

    async def scripted_get_text(path):
        calls["paths"].append(path)
        if status >= 400:
            raise RuntimeError(f"{status}: no diff yet — too early")
        return text

    monkeypatch.setattr(mcp, "_get_text", scripted_get_text)
    return calls


def _setup_post(monkeypatch, story: dict | None = None):
    captured: dict = {}

    async def scripted_post(path, body=None):
        captured["path"] = path
        captured["body"] = body
        return story or _story(status="done")

    monkeypatch.setattr(mcp, "_post", scripted_post)
    return captured


# ---- _get_text (plain-text GET helper) ----------------------------------


class _FakeResponse:
    def __init__(self, status: int, text: str, body: dict | None):
        self.status_code = status
        self.text = text
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def _patch_http(monkeypatch, response: _FakeResponse):
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, path):
            return response

    monkeypatch.setattr(mcp.httpx, "AsyncClient", FakeClient)


def test_get_text_returns_plain_body(monkeypatch):
    _patch_http(monkeypatch, _FakeResponse(200, "diff --git a/x b/x\n", None))
    out = asyncio.run(mcp._get_text("/stories/s1/diff"))
    assert out == "diff --git a/x b/x\n"


def test_get_text_401_gives_token_hint(monkeypatch):
    _patch_http(
        monkeypatch,
        _FakeResponse(401, "unauthorized", {"detail": "Not authenticated"}),
    )
    try:
        asyncio.run(mcp._get_text("/stories/s1/diff"))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "FUBALA_API_TOKEN" in str(e)


def test_get_text_error_surfaces_status_and_detail(monkeypatch):
    _patch_http(
        monkeypatch,
        _FakeResponse(404, "Not Found", {"detail": "no base_ref"}),
    )
    try:
        asyncio.run(mcp._get_text("/stories/s1/diff"))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert str(e) == "404: no base_ref"


# ---- get_story_diff ------------------------------------------------------


def test_diff_short_returns_raw(monkeypatch):
    d = _diff()
    _setup_get_text(monkeypatch, d)
    out = asyncio.run(mcp.get_story_diff("story-1"))
    assert out == d  # no notice, no truncation


def test_diff_long_truncates_with_exact_per_file_counts(monkeypatch):
    d = _diff()
    _setup_get_text(monkeypatch, d)
    out = asyncio.run(mcp.get_story_diff("story-1", max_chars=200))
    assert f"{len(d)} chars total" in out  # notice with total size
    assert out.endswith(d[:200])  # truncated head
    lines = out.splitlines()
    # exact per-file counts, +/- excluding the +++/--- header lines
    assert "- apples.py (+1/-1)" in lines
    assert "- cherries.py (+3/-0)" in lines
    assert "- dates.py (+0/-2)" in lines


def test_diff_404_surfaces_as_message(monkeypatch):
    _setup_get_text(monkeypatch, "", status=404)
    out = asyncio.run(mcp.get_story_diff("story-1"))  # must not raise
    assert out.startswith("no diff available: 404:")


def test_diff_hits_the_diff_endpoint(monkeypatch):
    calls = _setup_get_text(monkeypatch, "tiny")
    asyncio.run(mcp.get_story_diff("story-1"))
    assert calls["paths"] == ["/stories/story-1/diff"]


# ---- get_story: ## Verify -------------------------------------------------


def test_verify_section_passed_with_fix_rounds_and_tail(monkeypatch):
    output = "\n".join(f"line-{i:02d}" for i in range(70))
    story = _story(
        verify_result={
            "passed": True,
            "command": "uv run pytest",
            "attempts": 2,
            "exit_code": 0,
            "output": output,
        }
    )
    _setup_get(monkeypatch, story)
    out = asyncio.run(mcp.get_story("story-1"))
    assert "## Verify" in out
    assert "- command: uv run pytest" in out
    assert "- result: PASSED (exit_code 0), fix rounds: 1" in out
    tail = [line for line in out.splitlines() if line.startswith("line-")]
    assert len(tail) == 60  # last ~60 lines of output
    assert "line-69" in out and "line-09" not in out


def test_verify_section_failed_zero_fix_rounds(monkeypatch):
    story = _story(
        verify_result={
            "passed": False,
            "command": "uv run pytest",
            "attempts": 1,
            "exit_code": 1,
            "output": "E 2 failed",
        }
    )
    _setup_get(monkeypatch, story)
    out = asyncio.run(mcp.get_story("story-1"))
    assert "- result: FAILED (exit_code 1), fix rounds: 0" in out
    assert "E 2 failed" in out


def test_verify_not_applicable(monkeypatch):
    story = _story(verify_result={"applicable": False})
    _setup_get(monkeypatch, story)
    out = asyncio.run(mcp.get_story("story-1"))
    assert "## Verify" in out
    assert "not applicable (info/prep)" in out


def test_verify_falls_back_to_evidence(monkeypatch):
    story = _story(
        verify_result=None,
        evidence={"verify": {"passed": True, "command": "make ci", "output": "all green"}},
    )
    _setup_get(monkeypatch, story)
    out = asyncio.run(mcp.get_story("story-1"))
    assert "## Verify" in out
    assert "- command: make ci" in out
    assert "- result: PASSED" in out
    assert "all green" in out
    assert "fix rounds" not in out  # external evidence has no attempts


# ---- get_story: review label ---------------------------------------------


def test_review_label_code_review_when_verify_has_passed_key(monkeypatch):
    story = _story(
        ai_review="code review verdict",
        verify_result={"passed": True, "command": "c", "attempts": 1, "exit_code": 0, "output": ""},
    )
    _setup_get(monkeypatch, story)
    out = asyncio.run(mcp.get_story("story-1"))
    assert "## Code review\ncode review verdict" in out
    assert "## AI review" not in out


def test_review_label_ai_review_without_verify(monkeypatch):
    story = _story(ai_review="spec review verdict", verify_result=None)
    _setup_get(monkeypatch, story)
    out = asyncio.run(mcp.get_story("story-1"))
    assert "## AI review\nspec review verdict" in out
    assert "## Code review" not in out


def test_review_label_ai_review_when_verify_lacks_passed_key(monkeypatch):
    story = _story(ai_review="spec review verdict", verify_result={"applicable": False})
    _setup_get(monkeypatch, story)
    out = asyncio.run(mcp.get_story("story-1"))
    assert "## AI review\nspec review verdict" in out
    assert "## Code review" not in out


# ---- story_action: approve payload forwarding ------------------------------


def test_approve_override_forwards_justification(monkeypatch):
    captured = _setup_post(monkeypatch)
    out = asyncio.run(
        mcp.story_action(
            "story-1",
            "approve",
            override_verify=True,
            justification="operator accepted the failed verify",
        )
    )
    assert "action failed" not in out
    assert captured["path"] == "/stories/story-1/approve"
    assert captured["body"] == {
        "override_verify": True,
        "justification": "operator accepted the failed verify",
    }


def test_approve_plain_forwards_empty_body(monkeypatch):
    captured = _setup_post(monkeypatch)
    asyncio.run(mcp.story_action("story-1", "approve"))
    assert captured["path"] == "/stories/story-1/approve"
    assert captured["body"] == {}


def test_approve_override_without_justification_is_guarded(monkeypatch):
    captured = _setup_post(monkeypatch)
    out = asyncio.run(
        mcp.story_action("story-1", "approve", override_verify=True)
    )
    assert out == "override_verify requires a justification"
    assert "path" not in captured  # _post never called


# ---- story_action: retry (push+PR landing) ---------------------------------


def test_retry_hits_the_retry_land_endpoint(monkeypatch):
    """action="retry" re-arms a landing via POST /stories/<id>/retry-land —
    the Gitea update-branch POST happens server-side, so the tool's contract
    is just the right path, the 'by' attribution and the story summary."""
    captured = _setup_post(
        monkeypatch,
        _story(
            status="landing",
            error=None,
            pr_url="http://git.example.com/acme/app/pulls/7",
        ),
    )
    out = asyncio.run(mcp.story_action("story-1", "retry", reason="conflict on base"))
    assert "action failed" not in out
    assert captured["path"] == "/stories/story-1/retry-land"
    assert captured["body"] == {"by": "conflict on base"}
    assert "- status: landing" in out
    # the pr_url is surfaced so the agent can follow the landing
    assert "http://git.example.com/acme/app/pulls/7" in out


def test_retry_without_reason_defaults_the_by_line(monkeypatch):
    captured = _setup_post(monkeypatch, _story(status="landing"))
    out = asyncio.run(mcp.story_action("story-1", "retry"))
    assert "action failed" not in out
    assert captured["body"] == {"by": "mcp"}


def test_retry_forwards_the_error_from_the_endpoint(monkeypatch):
    async def failing_post(path, body=None):
        raise RuntimeError("409: only landing or human-review stories can retry a land")

    monkeypatch.setattr(mcp, "_post", failing_post)
    out = asyncio.run(mcp.story_action("story-1", "retry"))
    assert out == "action failed: 409: only landing or human-review stories can retry a land"


# ---- docstrings (gate guidance) -------------------------------------------


def test_gate_docstrings_mention_diff_and_verify_before_approve():
    diff_doc = (mcp.get_story_diff.__doc__ or "").lower()
    assert "diff" in diff_doc and "verify" in diff_doc
    assert "approve" in diff_doc
    action_doc = (mcp.story_action.__doc__ or "").lower()
    assert "approve" in action_doc
    assert "diff" in action_doc and "verify" in action_doc
    assert "get_story_diff" in action_doc  # approve references the diff tool
    story_doc = (mcp.get_story.__doc__ or "").lower()
    assert "verify" in story_doc and "get_story_diff" in story_doc
