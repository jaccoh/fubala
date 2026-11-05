"""Deterministic spec preflight, AC coverage, placeholder stripping.

The AI spec-review gate's local half: an incomplete, truncated or
placeholder-only spec must be refused before any LLM call; the reviewer must
account for every acceptance criterion; no provider placeholder line may ever
be stored in a deliverable.
"""

import time

import pytest


# ---- fixtures -------------------------------------------------------------

GOOD_SPEC = (
    "## Goal\nAdd the gate.\n\n"
    "## Context\nrunner reviews specs.\n\n"
    "## Implementation steps\n1. Add `_spec_preflight`\n2. Wire the gate\n\n"
    "## Risks & edge cases\n- false rejections burn requeue cycles\n\n"
    "## Acceptance checks\n- [ ] truncated spec rejected\n"
)

# every section present, but the text stops inside an unclosed code fence
TRUNCATED_SPEC = (
    "## Goal\nAdd the gate.\n\n"
    "## Implementation steps\n1. Do it\n\n"
    "## Acceptance checks\n- [ ] it works\n\n"
    "```python\ndef broken("
)

MID_SENTENCE_SPEC = (
    "## Goal\nAdd the gate.\n\n"
    "## Implementation steps\n1. Do it\n\n"
    "## Acceptance checks\n- [ ] it works\n\n"
    "The deadline is `future[-1].start +"
)

MISSING_SECTION_SPEC = "## Goal\nAdd the gate.\n## Implementation steps\n1. Do it\n"

PLACEHOLDER_ONLY = (
    "[System: read too far — skip loops detection reached limit — stopping]\n"
    "[System: thinking]\n"
)

# an info deliverable IS the finished document: no Implementation steps, no
# Acceptance checks — and that must pass its (kind-aware) preflight
INFO_DOC = (
    "# Runbook: restoring the DB\n\n"
    "The DB lives at `backend/data/fubala.db`.\n\n"
    "## Restore steps\n\n"
    "1. Stop the server.\n"
    "2. Copy the backup file back.\n"
    "3. Restart and check `/api/v1/health`.\n"
)

# this story's own acceptance text: numbered items, each with indented
# sub-bullets and wrapped prose — the shape the AC numbering must respect
STORY_AC_TEXT = """1. Deterministic pre-check before the LLM reviewer runs (red first). A spec fails when it:
   - misses a required section (the headings the spec-writer prompt demands, at least Goal, Implementation steps, Acceptance checks);
   - ends truncated: an unbalanced code fence or backtick span, or a last line that ends mid-sentence (no sentence-final punctuation, list item or checkbox);
   - is empty after placeholder stripping.

   A failing pre-check gives verdict rejected with a machine-readable reason ("spec truncated", "missing section: Acceptance checks"), without an LLM call. It counts as an AI rejection for the reject cap of story 5c8b5b70 if that exists. Tests: a truncated spec, a missing section, a placeholder-only spec, and a good spec that passes.
2. AC coverage. The reviewer prompt includes the story's acceptance criteria as a numbered list, and the reviewer must state for each AC where the spec covers it; any uncovered AC means rejected. Test on the built prompt, plus parsing of a fake reviewer reply that leaves one AC uncovered, which must be rejected.
3. Placeholder stripping. Provider placeholder lines (lines starting with "[System:" from z.ai's anthropic endpoint) are removed from the spec, the coder summary and the review text before they are stored. One shared helper (agent._meaningful already knows the pattern), with a test.
4. Happy path unchanged; full backend suite green; no TODO/placeholder."""


# ---- shared placeholder helper -------------------------------------------


def test_strip_placeholders_drops_system_lines_only():
    from app.agent import strip_placeholders

    text = "[System: thinking]\nkeep me\n\n[System: more]\nkeep me too\n"
    assert strip_placeholders(text) == "keep me\n\nkeep me too\n"


def test_strip_placeholders_is_round_trip_safe():
    from app.agent import strip_placeholders

    assert strip_placeholders("") == ""
    assert strip_placeholders(None) == ""
    assert strip_placeholders("clean\ntext\n") == "clean\ntext\n"
    assert strip_placeholders(GOOD_SPEC) == GOOD_SPEC


def test_strip_placeholders_matches_the_meaningful_predicate():
    from app.agent import _meaningful, strip_placeholders

    text = "[System: x]\nreal\n"
    assert _meaningful(strip_placeholders(text))
    assert not _meaningful(strip_placeholders("[System: x]\n"))


# ---- helper level: _missing_spec_section / _spec_sections_ok -------------


def test_missing_spec_section_returns_canonical_heading():
    from app.runner import _missing_spec_section

    assert _missing_spec_section(MISSING_SECTION_SPEC) == "## Acceptance checks"
    assert _missing_spec_section("## Goal\nx\n## acceptance checks\ny") == (
        "## Implementation steps"
    )
    assert _missing_spec_section(GOOD_SPEC) is None
    # empty text reports the first missing section, not a crash
    assert _missing_spec_section("") == "## Goal"
    assert _missing_spec_section(None) == "## Goal"


def test_spec_sections_ok_is_a_thin_wrapper():
    from app.runner import _spec_sections_ok

    assert _spec_sections_ok(GOOD_SPEC)
    assert _spec_sections_ok("## goal\na\n## implementation steps\nb\n## Acceptance Checks")
    assert not _spec_sections_ok(MISSING_SECTION_SPEC)
    assert not _spec_sections_ok("[System: thinking]")


# ---- helper level: _spec_preflight ----------------------------------------


def test_preflight_passes_a_good_spec():
    from app.runner import _spec_preflight

    assert _spec_preflight(GOOD_SPEC) is None


def test_preflight_rejects_missing_section_with_machine_readable_reason():
    from app.runner import _spec_preflight

    assert _spec_preflight(MISSING_SECTION_SPEC) == "missing section: Acceptance checks"


def test_preflight_rejects_unbalanced_code_fence():
    from app.runner import _spec_preflight

    assert _spec_preflight(TRUNCATED_SPEC) == "spec truncated"


def test_preflight_rejects_last_line_ending_mid_sentence():
    from app.runner import _spec_preflight

    assert _spec_preflight(MID_SENTENCE_SPEC) == "spec truncated"


def test_preflight_accepts_one_literal_backtick_inside_a_fence():
    """A backtick inside a balanced code fence is content, not markup — an
    odd inline count there must never read as an unterminated span."""
    from app.runner import _spec_preflight

    spec = (
        "## Goal\nAdd the gate.\n\n"
        "## Context\n"
        "```python\n"
        "# the ` character is literal in here\n"
        "```\n\n"
        "## Implementation steps\n1. Add the helper\n2. Wire the gate\n\n"
        "## Risks & edge cases\n- false rejections burn requeue cycles\n\n"
        "## Acceptance checks\n- [ ] passes\n"
    )
    assert _spec_preflight(spec) is None


def test_preflight_accepts_one_literal_backtick_in_prose():
    """One lone backtick in prose with a terminal last line is not
    truncation: an odd inline count alone must not reject a complete spec."""
    from app.runner import _spec_preflight

    spec = GOOD_SPEC.replace("runner reviews specs.", "the ` character")
    assert spec != GOOD_SPEC
    assert spec.count("`") % 2 == 1
    assert _spec_preflight(spec) is None


def test_preflight_rejects_bare_trailing_section_heading():
    """A spec ending right on '## Acceptance checks' has no checks at all —
    mid-document by definition."""
    from app.runner import _spec_preflight

    spec = "## Goal\nx\n## Implementation steps\n1. y\n## Acceptance checks"
    assert _spec_preflight(spec) == "spec truncated"


def test_preflight_rejects_placeholder_only_spec():
    from app.runner import _spec_preflight

    assert (
        _spec_preflight(PLACEHOLDER_ONLY) == "spec empty after placeholder stripping"
    )
    assert _spec_preflight("") == "spec empty after placeholder stripping"
    assert _spec_preflight(None) == "spec empty after placeholder stripping"


def test_preflight_strips_leading_placeholder_lines():
    from app.runner import _spec_preflight

    assert _spec_preflight("[System: skip loops]\n" + GOOD_SPEC) is None


def test_preflight_accepts_list_items_and_indented_last_lines():
    """Only clear truncation signals reject: a list item, a checkbox and an
    indented continuation are all legitimate final lines."""
    from app.runner import _spec_preflight

    for tail in ("- [ ] done\n", "- a bullet\n", "  wrapped continuation\n"):
        spec = (
            "## Goal\nx\n## Implementation steps\n1. y\n## Acceptance checks\n"
            "- [ ] a\n" + tail
        )
        assert _spec_preflight(spec) is None, tail


def test_preflight_is_kind_aware():
    """Info/prep deliverables owe no Implementation steps or Acceptance
    checks — their preflight checks only empty and truncated."""
    from app.models import StoryKind
    from app.runner import _spec_preflight

    assert _spec_preflight(INFO_DOC, StoryKind.INFO) is None
    assert _spec_preflight(INFO_DOC, StoryKind.PREP) is None
    # but the same checks still bind: truncated and placeholder-only
    assert _spec_preflight(MID_SENTENCE_SPEC, StoryKind.INFO) == "spec truncated"
    assert _spec_preflight(PLACEHOLDER_ONLY, StoryKind.INFO) == (
        "spec empty after placeholder stripping"
    )
    # and a code story keeps owing the mandatory sections
    assert _spec_preflight(INFO_DOC, StoryKind.CODE) == "missing section: Goal"


# ---- helper level: AC splitting -------------------------------------------


def test_split_acceptance_groups_numbered_items_with_sub_bullets():
    from app.runner import _split_acceptance

    acs = _split_acceptance(STORY_AC_TEXT)
    assert len(acs) == 4, "this story's own acceptance text is 4 criteria"
    assert acs[0].startswith("1. Deterministic pre-check")
    # the sub-bullets and wrapped prose belong to AC 1, not to their own ACs
    assert "misses a required section" in acs[0]
    assert "machine-readable reason" in acs[0]
    assert acs[1].startswith("2. AC coverage")
    assert acs[3].startswith("4. Happy path")


def test_split_acceptance_without_numbering_is_one_ac_per_line():
    from app.runner import _split_acceptance

    acs = _split_acceptance("first criterion\nsecond criterion\n\nthird\n")
    assert acs == ["first criterion", "second criterion", "third"]
    assert _split_acceptance("") == []
    assert _split_acceptance(None) == []
    assert _split_acceptance("   \n\n") == []


# ---- helper level: _uncovered_ac ------------------------------------------


def test_uncovered_ac_finds_the_first_explicit_gap():
    from app.runner import _uncovered_ac

    review = (
        "Looks solid overall.\n"
        "AC 1: COVERED — the spec adds the preflight.\n"
        "AC 2: NOT COVERED — no coverage instruction in the prompt.\n"
        "AC 3: COVERED — shared helper.\n"
        "VERDICT: APPROVED"
    )
    assert _uncovered_ac(review, 3) == "AC 2 not covered by spec"


def test_uncovered_ac_ignores_covered_and_out_of_range():
    from app.runner import _uncovered_ac

    assert _uncovered_ac("AC 1: COVERED — everywhere.\nVERDICT: APPROVED", 1) is None
    assert _uncovered_ac("AC 1: covered — everywhere.", 1) is None
    # AC 9 does not exist on this story: not a coverage verdict we honour
    assert _uncovered_ac("AC 9: NOT COVERED — nope", 4) is None


def test_uncovered_ac_without_a_coverage_block_is_none():
    """Fail-open: a reply with no coverage block falls back to the verdict
    line (pinned by test_stories.py)."""
    from app.runner import _uncovered_ac

    assert _uncovered_ac("Nice spec.\nVERDICT: APPROVED", 4) is None
    assert _uncovered_ac("", 4) is None


# ---- the built review prompt ----------------------------------------------


def _story(acceptance: str | None):
    from app.models import Story

    return Story(
        id="t1",
        title="Gate story",
        description="Add the gate.",
        acceptance=acceptance,
        spec=GOOD_SPEC,
    )


def test_review_prompt_numbers_the_acs_and_demands_coverage():
    from app.runner import _build_review_prompt

    prompt = _build_review_prompt(_story(STORY_AC_TEXT))
    assert "## Acceptance criteria" in prompt
    # exactly the four numbered criteria, sub-bullets kept inside their AC
    assert "1. Deterministic pre-check before the LLM reviewer runs" in prompt
    assert "4. Happy path unchanged" in prompt
    assert "5." not in prompt
    assert "## Coverage report" in prompt
    assert "AC <n>: COVERED" in prompt
    assert "AC <n>: NOT COVERED" in prompt


def test_review_prompt_renumbers_already_numbered_acs():
    """A story whose acceptance is already numbered 1..n must not come out as
    '1. 1. text' — the number is replaced, not stacked."""
    from app.runner import _build_review_prompt, _split_acceptance

    prompt = _build_review_prompt(_story(STORY_AC_TEXT))
    assert "1. 1." not in prompt
    assert len(_split_acceptance(STORY_AC_TEXT)) == 4
    assert "2. AC coverage." in prompt


def test_review_prompt_without_acs_has_no_coverage_section():
    from app.runner import _build_review_prompt

    prompt = _build_review_prompt(_story(None))
    assert "(none provided)" in prompt
    assert "Coverage report" not in prompt
    assert "AC <n>" not in prompt


# ---- integration: the gate itself ----------------------------------------


class ScriptedGateway:
    """Replays scripted rounds; records every stream_chat call."""

    def __init__(self, rounds: list[dict]):
        self.rounds = [dict(r) for r in rounds]
        self.calls: list[dict] = []

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
        from app.providers import Delta, Usage

        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "tools": tools,
                "max_output_tokens": max_output_tokens,
            }
        )
        index = min(len(self.calls) - 1, len(self.rounds) - 1)
        round_ = self.rounds[index]
        for chunk in round_.get("deltas", []):
            yield Delta(text=chunk)
        if round_.get("usage"):
            yield round_["usage"]
        elif "usage" not in round_:
            yield Usage(prompt_tokens=1, completion_tokens=1)


def _usage(completion_tokens: int = 1, finish_reason: str | None = None):
    from app.providers import Usage

    return Usage(
        prompt_tokens=5, completion_tokens=completion_tokens, finish_reason=finish_reason
    )


def _app_with(gateway, monkeypatch, tmp_path, name: str):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / f"{name}.db"))
    client = TestClient(create_app(gateway=gateway))
    client.__enter__()
    client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
    return client


def create_story(client, **body) -> dict:
    res = client.post("/api/v1/stories", json={"title": "Preflight story", **body})
    assert res.status_code == 201
    return res.json()


def wait_for_status(client, story_id: str, want: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    story = None
    while time.time() < deadline:
        story = client.get(f"/api/v1/stories/{story_id}").json()
        if story["status"] == want:
            return story
        time.sleep(0.05)
    pytest.fail(f"story never reached {want}; last: {story and story['status']}")


def _set_for_review(client, story_id: str, spec: str, status: str = "ai_review"):
    """Place a story in ai_review with the spec under test, bypassing the
    spec-writing phase (its own guard is tested elsewhere)."""
    from sqlmodel import Session

    from app.models import Story

    with Session(client.app.state.engine) as db:
        story = db.get(Story, story_id)
        story.status = status
        story.spec = spec
        db.add(story)
        db.commit()


def _note_reasons(client, story_id: str) -> list[str]:
    import json as _json

    events = client.get(f"/api/v1/stories/{story_id}").json()["events"]
    return [
        _json.loads(e["data"])["reason"]
        for e in events
        if e["kind"] == "note"
        and isinstance(_json.loads(e["data"]), dict)
        and _json.loads(e["data"]).get("reason")
    ]


def _review_calls(gateway) -> list[dict]:
    """The calls that went to the AI reviewer (not the spec writer)."""
    return [
        c
        for c in gateway.calls
        if (c["messages"][-1].get("content") or "").startswith("=== AI REVIEW REQUEST ===")
    ]


def _wait_for_ai_rejection(client, story_id: str, reason: str, timeout: float = 30.0) -> dict:
    """Wait for the ai-reviewer's rejection note (the status it causes —
    queued — is transient: the requeued spec phase runs immediately)."""
    deadline = time.time() + timeout
    story = None
    while time.time() < deadline:
        story = client.get(f"/api/v1/stories/{story_id}").json()
        if reason in _note_reasons(client, story_id):
            # re-fetch: the story read above may predate the rejection commit
            return client.get(f"/api/v1/stories/{story_id}").json()
        time.sleep(0.05)
    pytest.fail(
        f"no ai-reviewer note '{reason}'; last status: {story and story['status']}"
    )


def _status_moves(client, story_id: str) -> list[tuple[str, str]]:
    import json as _json

    events = client.get(f"/api/v1/stories/{story_id}").json()["events"]
    return [
        (_json.loads(e["data"]).get("from"), _json.loads(e["data"]).get("to"))
        for e in events
        if e["kind"] == "status"
    ]


def test_truncated_spec_is_rejected_without_an_llm_call(tmp_path, monkeypatch):
    gateway = ScriptedGateway([{"deltas": ["VERDICT: APPROVED"], "usage": _usage(400)}])
    client = _app_with(gateway, monkeypatch, tmp_path, "gate-truncated")
    story = create_story(client)
    _set_for_review(client, story["id"], TRUNCATED_SPEC)

    rejected = _wait_for_ai_rejection(client, story["id"], "spec truncated")
    assert rejected["ai_verdict"] == "rejected"
    assert rejected["ai_review"] == "VERDICT: REJECTED - spec truncated"
    # the rejection requeues the story (the spec runs again from there)
    assert ("ai_review", "queued") in _status_moves(client, story["id"])
    # the note is authored by the ai-reviewer, not the operator
    import json as _json

    events = client.get(f"/api/v1/stories/{story['id']}").json()["events"]
    by = [
        _json.loads(e["data"]).get("by")
        for e in events
        if e["kind"] == "note" and "by" in _json.loads(e["data"])
    ]
    assert "ai-reviewer" in by
    # zero reviewer calls: the gate refused locally (anything the gateway saw
    # afterwards is the requeued spec run, never a review)
    assert _review_calls(gateway) == []


def test_missing_section_spec_is_rejected_without_an_llm_call(tmp_path, monkeypatch):
    gateway = ScriptedGateway([{"deltas": ["VERDICT: APPROVED"], "usage": _usage(400)}])
    client = _app_with(gateway, monkeypatch, tmp_path, "gate-section")
    story = create_story(client)
    _set_for_review(client, story["id"], MISSING_SECTION_SPEC)

    rejected = _wait_for_ai_rejection(
        client, story["id"], "missing section: Acceptance checks"
    )
    assert rejected["ai_verdict"] == "rejected"
    assert "missing section: Acceptance checks" in rejected["ai_review"]
    assert ("ai_review", "queued") in _status_moves(client, story["id"])
    assert _review_calls(gateway) == []


def test_placeholder_only_spec_is_rejected_as_empty(tmp_path, monkeypatch):
    gateway = ScriptedGateway([{"deltas": ["VERDICT: APPROVED"], "usage": _usage(400)}])
    client = _app_with(gateway, monkeypatch, tmp_path, "gate-placeholder")
    story = create_story(client)
    _set_for_review(client, story["id"], PLACEHOLDER_ONLY)

    rejected = _wait_for_ai_rejection(
        client, story["id"], "spec empty after placeholder stripping"
    )
    assert rejected["ai_verdict"] == "rejected"
    assert "spec empty after placeholder stripping" in rejected["ai_review"]
    assert ("ai_review", "queued") in _status_moves(client, story["id"])
    assert _review_calls(gateway) == []


def test_good_spec_reaches_the_llm_reviewer_unchanged(tmp_path, monkeypatch):
    review = "Solid spec.\nAC 1: COVERED — goal section.\nVERDICT: APPROVED"
    gateway = ScriptedGateway([{"deltas": [review], "usage": _usage(50)}])
    client = _app_with(gateway, monkeypatch, tmp_path, "gate-good")
    story = create_story(client)
    _set_for_review(client, story["id"], GOOD_SPEC)

    human = wait_for_status(client, story["id"], "human_review")
    assert _review_calls(gateway), "the LLM reviewer must run for a good spec"
    assert human["ai_verdict"] == "approved"
    assert human["ai_review"] == review


def test_info_story_document_passes_preflight_and_is_reviewed(tmp_path, monkeypatch):
    """An info story's deliverable is the finished document — no Implementation
    steps or Acceptance checks — and the LLM reviewer still judges it."""
    review = "Clear runbook.\nVERDICT: APPROVED"
    gateway = ScriptedGateway([{"deltas": [review], "usage": _usage(50)}])
    client = _app_with(gateway, monkeypatch, tmp_path, "gate-info")
    story = create_story(client, kind="info")
    _set_for_review(client, story["id"], INFO_DOC)

    human = wait_for_status(client, story["id"], "human_review")
    assert _review_calls(gateway), "info deliverable must reach the LLM reviewer"
    assert human["ai_verdict"] == "approved"
    assert human["ai_review"] == review


def test_reviewer_reply_with_an_uncovered_ac_is_rejected(tmp_path, monkeypatch):
    review = (
        "Mostly good.\n"
        "AC 1: COVERED — the spec adds the gate.\n"
        "AC 2: NOT COVERED — the prompt has no coverage instruction.\n"
        "AC 3: COVERED — shared helper.\n"
        "AC 4: COVERED — suite green.\n"
        "VERDICT: APPROVED"
    )
    gateway = ScriptedGateway([{"deltas": [review], "usage": _usage(80)}])
    client = _app_with(gateway, monkeypatch, tmp_path, "gate-coverage")
    story = create_story(client, acceptance=STORY_AC_TEXT)
    _set_for_review(client, story["id"], GOOD_SPEC)

    queued = _wait_for_ai_rejection(client, story["id"], "AC 2 not covered by spec")
    assert queued["ai_verdict"] == "rejected"
    assert "AC 2: NOT COVERED" in queued["ai_review"]
    assert ("ai_review", "queued") in _status_moves(client, story["id"])
    # the reviewer ran exactly once, and saw the numbered ACs plus the
    # coverage instruction
    assert len(_review_calls(gateway)) == 1
    asked = _review_calls(gateway)[0]["messages"][-1]["content"]
    assert "1. Deterministic pre-check before the LLM reviewer runs" in asked
    assert "AC <n>: COVERED" in asked


def test_reviewer_reply_without_a_coverage_block_stays_fail_open(tmp_path, monkeypatch):
    """No coverage block, no verdict line: the spec review stays fail-open and
    the story reaches the human gate (pinned behaviour)."""
    gateway = ScriptedGateway([{"deltas": ["Looks fine to me."], "usage": _usage(20)}])
    client = _app_with(gateway, monkeypatch, tmp_path, "gate-failopen")
    story = create_story(client, acceptance=STORY_AC_TEXT)
    _set_for_review(client, story["id"], GOOD_SPEC)

    human = wait_for_status(client, story["id"], "human_review")
    assert human["ai_verdict"] == "approved"


def test_no_placeholder_line_survives_into_a_stored_deliverable(tmp_path, monkeypatch):
    """A placeholder-prefixed spec (from the writer) and a placeholder-prefixed
    review are both stored stripped — _finish is the single choke point."""
    review = (
        "[System: skip loops detection reached limit — stopping]\n"
        "Good spec.\n"
        "AC 1: COVERED — goal section.\n"
        "VERDICT: APPROVED"
    )
    gateway = ScriptedGateway(
        [{"deltas": ["[System: thinking]\n" + GOOD_SPEC], "usage": _usage(400)}]
    )
    client = _app_with(gateway, monkeypatch, tmp_path, "gate-strip")
    story = create_story(client)
    # the spec phase runs first (its reply becomes story.spec via _finish),
    # then the review round reads that stored spec
    gateway.rounds.append({"deltas": [review], "usage": _usage(50)})
    client.post(f"/api/v1/stories/{story['id']}/queue")

    human = wait_for_status(client, story["id"], "human_review")
    assert "[System:" not in human["spec"]
    assert human["spec"].startswith("## Goal")
    assert "[System:" not in human["ai_review"]
    assert human["ai_review"].startswith("Good spec.")


def test_evidence_summary_strips_provider_placeholders(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "ev-strip.db"))
    gateway = ScriptedGateway([])
    with TestClient(create_app(gateway=gateway)) as client:
        story = create_story(client)
        summary = "[System: thinking]\nLanded the gate and tests pass.\n[System: x]"
        res = client.post(
            f"/api/v1/stories/{story['id']}/evidence",
            json={"code_diff": "diff --git a/x b/x", "summary": summary},
        )
        assert res.status_code == 200
        assert res.json()["summary"] == "Landed the gate and tests pass."
        detail = client.get(f"/api/v1/stories/{story['id']}").json()
        assert detail["evidence"]["summary"] == "Landed the gate and tests pass."
        assert "[System:" not in detail["evidence"]["summary"]
