"""Claude Max: the phases that used to call the gateway WITH tools.

A claude_code provider cannot run Fubala's tool loop (the OAuth token only
works through the Claude Code CLI), so the spec phase runs as one CLI agent
session in the project root with read-only built-ins, and a pump (internal)
CODE stage is forced onto the headless executor. Live bug 29 Oct: every story
failed with "claude_code does not support tool calls". The subprocess is
mocked; no real CLI or token.
"""

import json
import time
from pathlib import Path

import pytest
from conftest import MINIMAL_SPEC
from test_headless import (
    SECRET_KEY,
    _events,
    _headless_client,
    _init_repo,
    _seed_provider,
    _wait_for_status,
)

from app.providers import claude_code as cc

TOKEN = "sk-ant-oat01-phase-test-token-0123456789"
MODEL = "claude-max/claude-sonnet-5-5"


def _spec_spawn(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_spawn(argv, *, env, cwd, stdin, timeout):
        calls.append({"argv": argv, "env": env, "cwd": cwd, "stdin": stdin})
        out = {
            "type": "result", "is_error": False, "result": MINIMAL_SPEC,
            "usage": {"input_tokens": 4, "output_tokens": 9},
        }
        return 0, json.dumps(out).encode(), b""

    monkeypatch.setattr(cc, "_spawn", fake_spawn)
    return calls


def _story(client, tmp_path: Path, **body) -> tuple[dict, dict]:
    client.post("/api/v1/providers/claude-max/key", json={"key": TOKEN})
    proj = client.post(
        "/api/v1/projects", json={"name": "demo", "root": _init_repo(tmp_path / "proj")}
    ).json()
    story = client.post(
        "/api/v1/stories",
        json={"title": "max", "project_id": proj["id"], "model": MODEL, **body},
    ).json()
    assert client.post(f"/api/v1/stories/{story['id']}/queue").status_code == 200
    return story, proj


def _wait_spec(client, story_id: str) -> dict:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        detail = client.get(f"/api/v1/stories/{story_id}").json()
        if detail.get("spec"):
            return detail
        time.sleep(0.1)
    raise AssertionError(f"no spec: {client.get(f'/api/v1/stories/{story_id}').json()}")


def test_spec_phase_is_one_cli_agent_session_in_the_project_root(
    tmp_path, monkeypatch, gateway
):
    calls = _spec_spawn(monkeypatch)
    client = _headless_client(tmp_path, monkeypatch, gateway)
    try:
        story, proj = _story(client, tmp_path)
        detail = _wait_spec(client, story["id"])
        assert "## Goal" in detail["spec"]
        call = calls[0]
        argv = call["argv"]
        assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
        assert "--max-turns" in argv
        assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
        assert Path(call["cwd"]).resolve() == Path(proj["root"]).resolve()
        assert call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
        assert "ANTHROPIC_AUTH_TOKEN" not in call["env"]
        assert b"=== SPEC REQUEST ===" in call["stdin"]
        # the tool loop never ran: no spec request reached the gateway
        gw = client.app.state.gateway
        assert not [
            c for c in gw.calls
            if str(c["messages"][-1].get("content", "")).startswith("=== SPEC REQUEST")
        ]
        # the token reached no event
        for e in _events(client, story["id"]):
            assert TOKEN not in e["raw"]
    finally:
        client.__exit__(None, None, None)


def test_pump_code_stage_is_forced_onto_the_headless_executor(
    tmp_path, monkeypatch, gateway
):
    _spec_spawn(monkeypatch)
    client = _headless_client(tmp_path, monkeypatch, gateway, "ok")
    try:
        story, _ = _story(client, tmp_path, execution="internal")
        _wait_for_status(client, story["id"], "human_review", 60)  # spec gate
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not [
            e for e in _events(client, story["id"], "note")
            if e["data"].get("executor") == "headless"
        ]:
            time.sleep(0.1)
        notes = [e["data"] for e in _events(client, story["id"], "note")]
        assert any(n.get("executor") == "headless" for n in notes), notes
        # Fubala's pump tool loop never reached the gateway for CODE
        gw = client.app.state.gateway
        assert not [
            c for c in gw.calls
            if str(c["messages"][-1].get("content", "")).startswith("=== CODE REQUEST")
        ]
    finally:
        client.__exit__(None, None, None)


# ---- a model and an --effort level per phase --------------------------------


def _flag(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def _phase_notes(client, story_id: str) -> dict[str, dict]:
    notes = [e["data"] for e in _events(client, story_id, "note")]
    return {n["phase"]: n for n in notes if "phase" in n}


def _review_calls(client) -> list[dict]:
    return [
        c for c in client.app.state.gateway.calls
        if str(c["messages"][-1].get("content", "")).startswith("=== AI REVIEW REQUEST")
    ]


def test_spec_and_review_default_efforts_reach_the_cli_and_the_events(
    tmp_path, monkeypatch, gateway
):
    calls = _spec_spawn(monkeypatch)
    client = _headless_client(tmp_path, monkeypatch, gateway)
    try:
        story, _ = _story(client, tmp_path)
        _wait_for_status(client, story["id"], "human_review", 60)
        assert _flag(calls[0]["argv"], "--effort") == "high"
        assert _flag(calls[0]["argv"], "--model") == "claude-sonnet-5-5"
        reviews = _review_calls(client)
        assert reviews and reviews[0]["effort"] == "medium"
        assert reviews[0]["model"] == MODEL
        notes = _phase_notes(client, story["id"])
        assert notes["spec"] == {"phase": "spec", "model": MODEL, "effort": "high"}
        assert notes["ai_review"] == {"phase": "ai_review", "model": MODEL, "effort": "medium"}
    finally:
        client.__exit__(None, None, None)


def test_empty_efforts_send_no_flag_anywhere(tmp_path, monkeypatch, gateway):
    calls = _spec_spawn(monkeypatch)
    client = _headless_client(
        tmp_path, monkeypatch, gateway, spec_effort="", review_effort="", code_effort=""
    )
    try:
        story, _ = _story(client, tmp_path)
        _wait_for_status(client, story["id"], "human_review", 60)
        assert "--effort" not in calls[0]["argv"]
        assert _review_calls(client)[0]["effort"] is None
        notes = _phase_notes(client, story["id"])
        assert notes["spec"]["effort"] is None
        assert notes["ai_review"]["effort"] is None
    finally:
        client.__exit__(None, None, None)


def test_spec_and_review_models_override_the_story_model_for_their_phase(
    tmp_path, monkeypatch, gateway
):
    opus, haiku = "claude-max/claude-opus-5-5", "claude-max/claude-haiku-5-5"
    calls = _spec_spawn(monkeypatch)
    client = _headless_client(
        tmp_path, monkeypatch, gateway, spec_model=opus, review_model=haiku
    )
    try:
        story, _ = _story(client, tmp_path)  # the story itself is on sonnet
        _wait_for_status(client, story["id"], "human_review", 60)
        assert _flag(calls[0]["argv"], "--model") == "claude-opus-5-5"
        assert _review_calls(client)[0]["model"] == haiku
        notes = _phase_notes(client, story["id"])
        assert notes["spec"]["model"] == opus
        assert notes["ai_review"]["model"] == haiku
        # the story row keeps its own model
        assert client.get(f"/api/v1/stories/{story['id']}").json()["model"] == MODEL
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("key", ["spec_model", "review_model"])
def test_unusable_phase_model_fails_the_story_naming_the_setting(
    tmp_path, monkeypatch, gateway, key
):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    unusable = "claude-max/claude-opus-5-5"  # no claude-max key in this install
    client = _headless_client(tmp_path, monkeypatch, gateway, **{key: unusable})
    try:
        zai_model = _seed_provider(client, SECRET_KEY)
        proj = client.post(
            "/api/v1/projects",
            json={"name": "demo", "root": _init_repo(tmp_path / "proj")},
        ).json()
        story = client.post(
            "/api/v1/stories",
            json={"title": "gate", "project_id": proj["id"], "model": zai_model},
        ).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        failed = _wait_for_status(client, story["id"], "failed", 60)
        assert key in failed["error"]
        assert unusable in failed["error"]
        # no silent fallback: nothing ran on the story model for that phase
        phase = "spec" if key == "spec_model" else "ai_review"
        assert phase not in _phase_notes(client, story["id"])
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("code_effort", "expected"), [("medium", "medium"), ("high", "high"), ("", None)]
)
def test_code_phase_effort_reaches_the_headless_child_and_its_note(
    tmp_path, monkeypatch, gateway, code_effort, expected
):
    _spec_spawn(monkeypatch)
    capture = tmp_path / "code-argv.txt"
    client = _headless_client(
        tmp_path, monkeypatch, gateway, "capture",
        fake_env={"FAKE_CLAUDE_CAPTURE": str(capture)}, code_effort=code_effort,
        spec_model="claude-max/claude-opus-5-5",  # must not leak into the code phase
    )
    try:
        story, _ = _story(client, tmp_path, execution="internal")
        _wait_for_status(client, story["id"], "human_review", 60)  # spec gate
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})

        def headless_notes() -> list[dict]:
            return [
                e["data"] for e in _events(client, story["id"], "note")
                if e["data"].get("executor") == "headless"
            ]

        # the note is recorded after the child exits, so wait for it (not
        # just for the argv capture, which is written when the child starts)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not headless_notes():
            time.sleep(0.1)
        argv = capture.read_text().split("\n")
        assert _flag(argv, "--effort") == expected
        notes = headless_notes()
        assert notes, "no headless note"
        assert notes[0]["model"] == MODEL
        assert notes[0]["effort"] == expected
    finally:
        client.__exit__(None, None, None)


def test_spec_nudge_keeps_the_spec_model_and_effort(tmp_path, monkeypatch, gateway):
    async def bad_spawn(argv, *, env, cwd, stdin, timeout):
        out = {"type": "result", "is_error": False, "result": "no sections here",
               "usage": {"input_tokens": 1, "output_tokens": 1}}
        return 0, json.dumps(out).encode(), b""

    monkeypatch.setattr(cc, "_spawn", bad_spawn)
    client = _headless_client(tmp_path, monkeypatch, gateway)
    try:
        story, _ = _story(client, tmp_path)
        _wait_for_status(client, story["id"], {"failed", "human_review"}, 60)
        notes = _phase_notes(client, story["id"])
        assert notes["spec_nudge"] == {
            "phase": "spec_nudge", "model": MODEL, "effort": "high",
        }
        nudges = [
            c for c in client.app.state.gateway.calls
            if "not a usable spec" in str(c["messages"][-1].get("content", ""))
        ]
        assert nudges and nudges[0]["effort"] == "high"
        assert nudges[0]["model"] == MODEL
    finally:
        client.__exit__(None, None, None)
