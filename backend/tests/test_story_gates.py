"""Per-story gates (human | auto) for the spec gate and the code gate.

An `auto` gate passes without a human only when every machine check is
green; any failed check parks the story at that human gate with the failed
checks named in a note (and in the `gate_auto` event).

Spec gate checks: ai_spec_review_approved, no_human_rejection.
Code gate checks: verify_green, ai_code_review_approved, no_protected_paths,
no_frozen_paths, diff_within_max, no_human_rejection.

The gate fires inside the runner's transitions, so the end-to-end cases run
the REAL pipeline (spec -> AI review -> coder worktree -> verify -> code
review) against a scripted gateway and a real git repo, exactly like
test_delivery_push.py; an API-only test could never reach them.
"""

import json
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from conftest import MINIMAL_SPEC
from fastapi.testclient import TestClient

from app.main import create_app

# ---- helpers --------------------------------------------------------------


def init_repo(tmp_path) -> str:
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return str(repo)


class GateScriptedGW:
    """Routes every runner phase on its prompt marker (never a call counter),
    so a story that passes the same stage twice cannot exhaust the script.

    Verdicts and the coder's write are mutable instance attributes so a test
    can flip a verdict BETWEEN runs (e.g. reject first, approve on the rerun).
    """

    def __init__(
        self,
        *,
        spec_verdict="approved",
        code_review_verdict="approved",
        coder_path="feature.py",
        coder_content="VALUE = 'gated'\n",
    ):
        self.spec_verdict = spec_verdict
        self.code_review_verdict = code_review_verdict
        self.coder_path = coder_path
        self.coder_content = coder_content
        self.calls: list[str] = []

    @staticmethod
    def _last_user_text(messages) -> str:
        for message in reversed(messages):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
        from app.providers import Delta as D
        from app.providers import ToolCall as TC
        from app.providers import Usage as U

        prompt = self._last_user_text(messages)
        after_tool_round = any(m.get("role") == "tool" for m in messages)
        if "=== SPEC REQUEST ===" in prompt:
            self.calls.append("spec")
            yield D(text=MINIMAL_SPEC)
        elif "=== AI REVIEW REQUEST ===" in prompt:
            self.calls.append("spec-review")
            yield D(text=f"fine\n\nVERDICT: {self.spec_verdict.upper()}")
        elif "=== CODE REQUEST ===" in prompt and not after_tool_round:
            self.calls.append("coder")
            yield TC(
                id="w1",
                name="write_file",
                arguments=json.dumps(
                    {"path": self.coder_path, "content": self.coder_content}
                ),
            )
        elif "=== CODE REQUEST ===" in prompt:
            self.calls.append("coder-final")
            yield D(text=f"implemented {self.coder_path}")
        else:
            self.calls.append("code-review")
            yield D(
                text=f"diff matches spec\n\nVERDICT: {self.code_review_verdict.upper()}"
            )
        yield U(prompt_tokens=1, completion_tokens=1)


@contextmanager
def gate_client(tmp_path, monkeypatch, db_name, *, gateway=None):
    """A TestClient against a fresh DB, a real repo as project root and write
    tools on — the minimum the pipeline needs to reach the gates."""
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / db_name))
    with TestClient(create_app(gateway=gateway or GateScriptedGW())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
            },
        )
        yield client, repo


def make_project(client, repo: str, **fields) -> dict:
    body = {"name": "p", "root": repo}
    body.update(fields)
    return client.post("/api/v1/projects", json=body).json()


def wait_status(client, sid, want, timeout=60.0) -> dict:
    deadline = time.time() + timeout
    cur = None
    while time.time() < deadline:
        cur = client.get(f"/api/v1/stories/{sid}").json()
        if cur["status"] in want:
            return cur
        time.sleep(0.05)
    pytest.fail(
        f"never reached {want}; last: {cur and cur['status']} {cur and cur.get('error')}"
    )


def detail(client, sid) -> dict:
    return client.get(f"/api/v1/stories/{sid}").json()


def gate_events(story_detail: dict) -> list[dict]:
    return [
        json.loads(e.get("data") or "{}")
        for e in story_detail.get("events", [])
        if e.get("kind") == "gate_auto"
    ]


class BadToolArgsGW(GateScriptedGW):
    """Incident e56b558d: the coder's tool-call arguments arrive as invalid
    JSON and the provider answers 400.

    Routes on the CODE REQUEST marker ANYWHERE in the conversation (house
    rule: never a call counter), because the corrective turn the pump
    appends becomes the last user message; the corrective-marker count
    itself decides how many 400s precede the valid tool call."""

    def __init__(self, *, bad_rounds=1, **kw):
        super().__init__(**kw)
        self.bad_rounds = bad_rounds
        self.four_hundreds = 0

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
        from app.providers import Delta as D
        from app.providers import GatewayError
        from app.providers import ToolCall as TC
        from app.providers import Usage as U

        in_coder = any(
            m.get("role") == "user"
            and "=== CODE REQUEST ===" in str(m.get("content") or "")
            for m in messages
        )
        corrections = sum(
            1
            for m in messages
            if m.get("role") == "user"
            and "invalid JSON arguments" in str(m.get("content") or "")
        )
        if in_coder:
            if corrections < self.bad_rounds:
                self.four_hundreds += 1
                raise GatewayError(
                    "litellm.BadRequestError: AnthropicException - Failed to "
                    "parse tool call arguments for tool 'edit_file' "
                    "Error: Expecting property name enclosed in double quotes: "
                    "line 1 column 2 (char 1)"
                )
            if not any(m.get("role") == "tool" for m in messages):
                self.calls.append("coder")
                yield TC(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps(
                        {"path": self.coder_path, "content": self.coder_content}
                    ),
                )
            else:
                self.calls.append("coder-final")
                yield D(text=f"implemented {self.coder_path}")
            yield U(prompt_tokens=1, completion_tokens=1)
            return
        async for event in GateScriptedGW.stream_chat(
            self, model=model, messages=messages, tools=tools,
            max_output_tokens=max_output_tokens,
        ):
            yield event


def note_events(story_detail: dict) -> list[dict]:
    return [
        json.loads(e.get("data") or "{}")
        for e in story_detail.get("events", [])
        if e.get("kind") == "note"
    ]


def failed_checks(payload: dict) -> list[str]:
    return [c["name"] for c in payload.get("checks", []) if not c["passed"]]


def held_notes(story_detail: dict) -> list[str]:
    """StoryNote bodies the auto gate wrote (author fubala, 'held' wording)."""
    return [
        n.get("body", "")
        for n in story_detail.get("notes", [])
        if n.get("author") == "fubala" and "held the story" in (n.get("body") or "")
    ]


def wait_gate(client, sid, gate: str, *, approved=None, timeout=60.0) -> dict:
    """Poll until a `gate_auto` event for `gate` exists.

    Necessary, not just tidy: on a held gate the status flips to
    human_review BEFORE the gate event and note commit, so an immediate
    fetch can legitimately see neither."""
    deadline = time.time() + timeout
    last: list[dict] = []
    while time.time() < deadline:
        last = [g for g in gate_events(detail(client, sid)) if g.get("gate") == gate]
        if last and (approved is None or last[-1]["approved"] is approved):
            return last[-1]
        time.sleep(0.05)
    pytest.fail(f"no {gate} gate_auto event (approved={approved}); saw {last}")


def assert_held(client, sid, gate, *checks) -> dict:
    """The gate held: status parked at human_review, the note and the event
    both name every check in `checks`.

    Waits for the gate event FIRST: at the code gate the story is still
    `executing` while the coder/verify/review rounds run, so an immediate
    status fetch races the pipeline. The gate_auto event is recorded when
    the gate actually decides (at or after the status flip), so once it
    exists the park has happened."""
    payload = wait_gate(client, sid, gate, approved=False)
    story = detail(client, sid)
    assert story["status"] == "human_review", story.get("error")
    assert payload["gate"] == gate
    for check in checks:
        assert check in failed_checks(payload), failed_checks(payload)
    notes = held_notes(story) or held_notes(detail(client, sid))
    assert notes, "the auto gate must write a parking note"
    for check in checks:
        assert check in notes[-1], notes[-1]
    return payload


# ---- API surface: create / PATCH / project defaults -----------------------


def test_story_defaults_to_human_without_a_project(client):
    story = client.post("/api/v1/stories", json={"title": "g"}).json()
    assert story["gate_spec"] == "human"
    assert story["gate_code"] == "human"
    assert story["human_rejected"] is False


def test_project_default_is_inherited_at_create(client, tmp_path):
    repo = init_repo(tmp_path)
    project = make_project(
        client, repo, default_gate_spec="auto", default_gate_code="auto"
    )
    story = client.post(
        "/api/v1/stories", json={"title": "g", "project_id": project["id"]}
    ).json()
    assert story["gate_spec"] == "auto"
    assert story["gate_code"] == "auto"
    # a per-story override still wins over the project default
    other = client.post(
        "/api/v1/stories",
        json={"title": "o", "project_id": project["id"], "gate_code": "human"},
    ).json()
    assert other["gate_spec"] == "auto"
    assert other["gate_code"] == "human"


def test_invalid_gate_value_is_a_422(client):
    for field in ("gate_spec", "gate_code"):
        res = client.post("/api/v1/stories", json={"title": "g", field: "nope"})
        assert res.status_code == 422, res.text
    story = client.post("/api/v1/stories", json={"title": "g"}).json()
    res = client.patch(f"/api/v1/stories/{story['id']}", json={"gate_spec": "nope"})
    assert res.status_code == 422, res.text
    ok = client.patch(f"/api/v1/stories/{story['id']}", json={"gate_code": "auto"})
    assert ok.status_code == 200


def test_gates_patchable_while_editable_locked_after(tmp_path, monkeypatch):
    with gate_client(tmp_path, monkeypatch, "patch-lock.db") as (client, repo):
        story = client.post("/api/v1/stories", json={"title": "g"}).json()
        # SPEC stays human (the story must park at the human spec gate for
        # the lock check below); only the CODE gate flips, proving the PATCH
        # accepts both fields while the story is editable.
        res = client.patch(
            f"/api/v1/stories/{story['id']}",
            json={"gate_code": "auto"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["gate_spec"] == "human" and body["gate_code"] == "auto"

        client.post(f"/api/v1/stories/{story['id']}/queue")
        parked = wait_status(client, story["id"], ("human_review", "failed"))
        assert parked["status"] == "human_review", parked.get("error")
        # at the gate the story is no longer editable (same guard as
        # `execution`): only backlog/queued/failed may change
        locked = client.patch(
            f"/api/v1/stories/{story['id']}", json={"gate_spec": "human"}
        )
        assert locked.status_code == 409


def test_reject_permanence_survives_patch(tmp_path, monkeypatch):
    with gate_client(
        tmp_path, monkeypatch, "reject-perm.db", gateway=GateScriptedGW(
            spec_verdict="rejected"
        )
    ) as (client, repo):
        story = client.post(
            "/api/v1/stories", json={"title": "g", "gate_spec": "auto"}
        ).json()
        sid = story["id"]
        client.post(f"/api/v1/stories/{sid}/queue")
        wait_status(client, sid, ("human_review", "failed"))
        # Park the scheduler BEFORE rejecting: with a 0.05s tick the runner
        # would re-pick the queued story (rejected verdict again -> back to
        # human_review, which is not editable) before the PATCH below could
        # land. `story_poll_interval` is validated le=60, so write a value
        # the schema accepts and wait out the tick already sleeping on the
        # old interval (the loop reads the interval once per iteration).
        parked = client.patch("/api/v1/settings", json={"story_poll_interval": 30})
        assert parked.status_code == 200, parked.text
        time.sleep(0.3)
        client.post(
            f"/api/v1/stories/{sid}/reject",
            json={"reason": "not what I asked for", "by": "owner"},
        )
        # queued and held: editable again, the gate may flip, the rejection
        # flag never can (update_story never writes that column)
        flipped = client.patch(
            f"/api/v1/stories/{sid}", json={"gate_code": "auto"}
        ).json()
        assert flipped["gate_code"] == "auto"
        assert flipped["human_rejected"] is True


def test_project_gate_fields_roundtrip(client, tmp_path):
    repo = init_repo(tmp_path)
    project = make_project(
        client,
        repo,
        default_gate_spec="auto",
        default_gate_code="human",
        protected_globs="**/migrations/**, infra/**",
        frozen_globs="**/pinned.py",
        auto_max_diff_lines=25,
    )
    for field, want in (
        ("default_gate_spec", "auto"),
        ("default_gate_code", "human"),
        ("protected_globs", "**/migrations/**, infra/**"),
        ("frozen_globs", "**/pinned.py"),
        ("auto_max_diff_lines", 25),
    ):
        assert project[field] == want, field
    patched = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={"default_gate_code": "auto", "auto_max_diff_lines": None},
    ).json()
    assert patched["default_gate_code"] == "auto"
    assert patched["auto_max_diff_lines"] == 400
    assert (
        client.patch(
            f"/api/v1/projects/{project['id']}", json={"default_gate_spec": "nope"}
        ).status_code
        == 422
    )
    fresh = make_project(client, repo)
    assert fresh["protected_globs"] == "" and fresh["frozen_globs"] == ""
    assert fresh["default_gate_spec"] == "human"


def test_migration_stamps_pre_existing_stories_human(tmp_path, monkeypatch):
    """Legacy rows (gate columns absent) are stamped human on the next boot —
    asserted on the raw columns, since the API read path coerces anyway."""
    db_path = tmp_path / "legacy-gates.db"
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
    with TestClient(create_app(gateway=object())) as client:
        created = client.post("/api/v1/stories", json={"title": "legacy"}).json()
        sid = created["id"]
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE stories DROP COLUMN gate_spec")
    conn.execute("ALTER TABLE stories DROP COLUMN gate_code")
    conn.execute("ALTER TABLE stories DROP COLUMN human_rejected")
    conn.commit()
    conn.close()
    with TestClient(create_app(gateway=object())) as _client:
        check = sqlite3.connect(db_path)
        row = check.execute(
            "SELECT gate_spec, gate_code, human_rejected FROM stories WHERE id=?",
            (sid,),
        ).fetchone()
        check.close()
        assert row == ("human", "human", 0)


# ---- spec gate auto -------------------------------------------------------


def test_auto_spec_gate_passes_without_a_human(tmp_path, monkeypatch):
    with gate_client(tmp_path, monkeypatch, "spec-auto.db") as (client, repo):
        project = make_project(client, repo, default_gate_spec="auto")
        story = client.post(
            "/api/v1/stories", json={"title": "g", "project_id": project["id"]}
        ).json()
        sid = story["id"]
        client.post(f"/api/v1/stories/{sid}/queue")

        moved = wait_status(client, sid, ("executing", "human_review", "failed"))
        assert moved["status"] == "executing", moved.get("error")
        assert moved["approved_by"] == "auto (gate)"
        gate = wait_gate(client, sid, "spec", approved=True)
        assert failed_checks(gate) == []
        # a passing gate writes no parking note
        assert not held_notes(detail(client, sid))
        # spec_approved rides the same path a human approve takes
        kinds = [e.get("kind") for e in detail(client, sid)["events"]]
        assert "spec_approved" in kinds


def test_auto_spec_gate_fails_on_ai_rejection_and_parks(tmp_path, monkeypatch):
    with gate_client(
        tmp_path, monkeypatch, "spec-rej.db",
        gateway=GateScriptedGW(spec_verdict="rejected"),
    ) as (client, repo):
        project = make_project(client, repo, default_gate_spec="auto")
        story = client.post(
            "/api/v1/stories", json={"title": "g", "project_id": project["id"]}
        ).json()
        sid = story["id"]
        client.post(f"/api/v1/stories/{sid}/queue")

        wait_status(client, sid, ("human_review", "failed"))
        assert_held(client, sid, "spec", "ai_spec_review_approved")
        # it parked at the SPEC gate: no worktree was ever created
        assert not detail(client, sid).get("base_ref")


def test_auto_spec_gate_blocked_by_prior_human_rejection(tmp_path, monkeypatch):
    """Reject at the spec gate, then an all-green rerun must still park —
    `human_rejected` is permanent and is a spec-gate check."""
    gw = GateScriptedGW(spec_verdict="rejected")
    with gate_client(tmp_path, monkeypatch, "spec-hrej.db", gateway=gw) as (client, repo):
        project = make_project(client, repo, default_gate_spec="auto")
        story = client.post(
            "/api/v1/stories", json={"title": "g", "project_id": project["id"]}
        ).json()
        sid = story["id"]
        client.post(f"/api/v1/stories/{sid}/queue")
        wait_status(client, sid, ("human_review", "failed"))
        client.post(
            f"/api/v1/stories/{sid}/reject",
            json={"reason": "wrong shape", "by": "owner"},
        )

        # the rerun is all-green machine-wise (the reviewer now approves)
        gw.spec_verdict = "approved"
        wait_status(client, sid, ("human_review", "failed", "done"))
        assert_held(client, sid, "spec", "no_human_rejection")
        assert detail(client, sid)["human_rejected"] is True
        assert not detail(client, sid).get("base_ref")


# ---- code gate auto: all green -------------------------------------------


def test_auto_code_gate_merge_reaches_done(tmp_path, monkeypatch):
    with gate_client(tmp_path, monkeypatch, "code-auto.db") as (client, repo):
        project = make_project(
            client, repo, default_gate_spec="auto", default_gate_code="auto"
        )
        story = client.post(
            "/api/v1/stories", json={"title": "g", "project_id": project["id"]}
        ).json()
        sid = story["id"]
        client.post(f"/api/v1/stories/{sid}/queue")
        final = wait_status(client, sid, ("done", "failed", "human_review"))
        assert final["status"] == "done", final.get("error")
        assert final["approved_by"] == "auto (gate)"
        assert [g["gate"] for g in gate_events(detail(client, sid))] == ["spec", "code"]
        assert all(g["approved"] for g in gate_events(detail(client, sid)))
        assert not held_notes(detail(client, sid))
        # merge delivery: the work landed in the root checkout
        assert (Path(repo) / "feature.py").exists()


def test_auto_code_gate_push_pr_lands_without_a_human(tmp_path, monkeypatch):
    """Push+PR delivery: the auto code gate lands through the same branch a
    human approve takes (push + PR) and reaches `landing`, never leaking the
    PR token."""
    from test_delivery_push import _fake_gitea, init_remote

    repo = init_repo(tmp_path)
    init_remote(tmp_path, repo)
    token_file = tmp_path / "token.txt"
    secret = "tok-gate-pr"
    token_file.write_text(secret + "\n")
    server = _fake_gitea()
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "code-pr.db"))
    try:
        with TestClient(create_app(gateway=GateScriptedGW())) as client:
            client.patch(
                "/api/v1/settings",
                json={
                    "story_poll_interval": 0.05,
                    "project_root": repo,
                    "write_tools_enabled": True,
                },
            )
            project = make_project(
                client,
                repo,
                default_gate_spec="auto",
                default_gate_code="auto",
                delivery="push",
                pr_api_base=f"http://127.0.0.1:{server.server_address[1]}",
                pr_repo="acme/app",
                pr_base_branch="main",
                pr_token_file=str(token_file),
            )
            story = client.post(
                "/api/v1/stories", json={"title": "g", "project_id": project["id"]}
            ).json()
            sid = story["id"]
            client.post(f"/api/v1/stories/{sid}/queue")
            final = wait_status(client, sid, ("landing", "done", "failed"))
            assert final["status"] == "landing", final.get("error")
            assert final["approved_by"] == "auto (gate)"
            assert final["pr_url"] == "http://gitea.local/acme/app/pulls/7"
            assert final["pr_index"] == 7
            story_detail = detail(client, sid)
            assert [g["gate"] for g in gate_events(story_detail)] == ["spec", "code"]
            assert all(g["approved"] for g in gate_events(story_detail))
            assert secret not in json.dumps(story_detail)
            assert secret not in json.dumps(final)
            posts = [r for r in server.requests if r["method"] == "POST"]
            assert len(posts) == 1, server.requests
            assert posts[0]["path"] == "/api/v1/repos/acme/app/pulls"
            assert posts[0]["body"]["head"] == f"fubala/{sid}"
            assert posts[0]["auth"] == f"token {secret}"
    finally:
        server.shutdown()
        server.server_close()


# ---- code gate auto: every check failing parks ----------------------------


@contextmanager
def code_gate_case(tmp_path, monkeypatch, db_name, *, gateway=None, **project_fields):
    """One story with a HUMAN spec gate and an AUTO code gate, driven to the
    code review transition; yields the client and the story id."""
    with gate_client(
        tmp_path, monkeypatch, db_name, gateway=gateway or GateScriptedGW()
    ) as (client, repo):
        project = make_project(client, repo, **project_fields)
        story = client.post(
            "/api/v1/stories",
            json={
                "title": "g",
                "project_id": project["id"],
                "gate_spec": "human",
                "gate_code": "auto",
            },
        ).json()
        sid = story["id"]
        client.post(f"/api/v1/stories/{sid}/queue")
        first = wait_status(client, sid, ("human_review", "failed"))
        assert first["status"] == "human_review", first.get("error")
        # the human passes the spec gate; the code gate then decides itself
        client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
        yield client, sid


@pytest.mark.parametrize(
    "case,project_fields,gateway_kwargs",
    [
        ("verify_green", {"verify_command": "exit 1"}, {}),
        ("ai_code_review_approved", {}, {"code_review_verdict": "rejected"}),
        ("no_protected_paths", {"protected_globs": "feature.py"}, {}),
        ("no_frozen_paths", {"frozen_globs": "*.py"}, {}),
        # cap 1 with a 3-line write: the diff must exceed the cap (a 1-line
        # write adds exactly one line and passes, landing the story)
        (
            "diff_within_max",
            {"auto_max_diff_lines": 1},
            {"coder_content": "A = 1\nB = 2\nC = 3\n"},
        ),
    ],
)
def test_each_failing_code_gate_check_parks(
    tmp_path, monkeypatch, case, project_fields, gateway_kwargs
):
    """Checks (a)-(e), one failing each: the story parks at human_review, the
    note names the check, the gate_auto event lists it failed."""
    with code_gate_case(
        tmp_path,
        monkeypatch,
        f"chk-{case}.db",
        gateway=GateScriptedGW(**gateway_kwargs),
        **project_fields,
    ) as (client, sid):
        wait_status(client, sid, ("human_review", "done", "failed"))
        assert_held(client, sid, "code", case)


def test_failing_no_human_rejection_at_code_gate_parks(tmp_path, monkeypatch):
    """Check (f): a human reject at the CODE gate, then an all-green rerun —
    the auto code gate still parks."""
    gw = GateScriptedGW()
    with gate_client(tmp_path, monkeypatch, "chk-hrej.db", gateway=gw) as (client, repo):
        project = make_project(client, repo, default_gate_code="human")
        story = client.post(
            "/api/v1/stories",
            json={"title": "g", "project_id": project["id"], "gate_spec": "human"},
        ).json()
        sid = story["id"]
        client.post(f"/api/v1/stories/{sid}/queue")
        first = wait_status(client, sid, ("human_review", "failed"))
        assert first["status"] == "human_review", first.get("error")
        client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
        second = wait_status(client, sid, ("human_review", "done", "failed"))
        assert second["status"] == "human_review", second.get("error")
        # Park the scheduler BEFORE rejecting: with a 0.05s tick the runner
        # would re-pick the queued story before the gate PATCH below could
        # land (executing is not editable -> 409). `story_poll_interval` is
        # validated le=60, so write a value the schema accepts and wait out
        # the tick already sleeping on the old interval.
        parked = client.patch("/api/v1/settings", json={"story_poll_interval": 30})
        assert parked.status_code == 200, parked.text
        time.sleep(0.3)
        client.post(f"/api/v1/stories/{sid}/reject", json={"reason": "no", "by": "o"})
        assert detail(client, sid)["human_rejected"] is True

        # queued again -> editable: switch the code gate to auto. The rerun
        # must also write NEW content: after a code-gate reject the coder is
        # held to the no-op guard, and re-writing the identical file would
        # park it for "no changes" instead of reaching the gate.
        gw.coder_content = "VALUE = 'gated after reject'\n"
        flipped = client.patch(f"/api/v1/stories/{sid}", json={"gate_code": "auto"})
        assert flipped.status_code == 200, flipped.text
        released = client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
        assert released.status_code == 200, released.text
        wait_status(client, sid, ("human_review", "done", "failed"))
        assert_held(client, sid, "code", "no_human_rejection")


def test_reject_at_spec_blocks_auto_code_gate_later(tmp_path, monkeypatch):
    """A reject at the SPEC gate is story-wide: even after a human pushes the
    spec through, the auto CODE gate must still park."""
    gw = GateScriptedGW(spec_verdict="rejected")
    with gate_client(tmp_path, monkeypatch, "cross-gate.db", gateway=gw) as (client, repo):
        project = make_project(
            client, repo, default_gate_spec="auto", default_gate_code="auto"
        )
        story = client.post(
            "/api/v1/stories", json={"title": "g", "project_id": project["id"]}
        ).json()
        sid = story["id"]
        client.post(f"/api/v1/stories/{sid}/queue")
        wait_status(client, sid, ("human_review", "failed"))
        # reject at the SPEC gate (no base_ref yet)
        assert not detail(client, sid).get("base_ref")
        client.post(f"/api/v1/stories/{sid}/reject", json={"reason": "no", "by": "owner"})

        # rerun all-green: the spec gate itself is held by the rejection
        gw.spec_verdict = "approved"
        wait_status(client, sid, ("human_review", "failed", "done"))
        assert_held(client, sid, "spec", "no_human_rejection")
        # a human can still push the spec through...
        client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
        # ...but the code gate stays closed to automation for good: wait for
        # the CODE gate's own decision (wait_status would return the moment
        # the status flips to executing, before the gate has even run)
        assert_held(client, sid, "code", "no_human_rejection")


# ---- evaluator units ------------------------------------------------------


def test_diff_helpers_count_and_paths():
    from app.runner import _diff_changed_paths, _diff_line_counts

    diff = "\n".join(
        [
            "diff --git a/apples.py b/apples.py",
            "--- a/apples.py",
            "+++ b/apples.py",
            "@@ -1,3 +1,3 @@",
            " context",
            "+added",
            "-removed",
            "diff --git a/old.py b/new.py",
            "--- a/old.py",
            "+++ b/new.py",
            "@@ -1,2 +1,2 @@",
            "+one",
            "-two",
        ]
    )
    added, removed = _diff_line_counts(diff)
    assert (added, removed) == (2, 2)
    # renames count as changed paths on both sides
    assert set(_diff_changed_paths(diff)) == {"apples.py", "old.py", "new.py"}


def test_evaluate_spec_gate_checks():
    from app.runner import evaluate_spec_gate

    green = evaluate_spec_gate(ai_verdict="approved", human_rejected=False)
    assert [c.name for c in green] == [
        "ai_spec_review_approved",
        "no_human_rejection",
    ]
    assert all(c.passed for c in green)
    red = evaluate_spec_gate(ai_verdict="rejected", human_rejected=True)
    assert [c.name for c in red if not c.passed] == [
        "ai_spec_review_approved",
        "no_human_rejection",
    ]


def test_evaluate_code_gate_checks():
    from app.runner import evaluate_code_gate

    diff = "\n".join(
        [
            "diff --git a/feature.py b/feature.py",
            "--- a/feature.py",
            "+++ b/feature.py",
            "@@ -1,2 +1,3 @@",
            "+one",
            "+two",
            "-old",
        ]
    )
    green = evaluate_code_gate(
        ai_verdict="approved",
        verify={"passed": True, "attempts": 1},
        diff_text=diff,
        human_rejected=False,
        protected_globs="migrations/**",
        frozen_globs="**/pinned.py",
        auto_max_diff_lines=400,
    )
    assert all(c.passed for c in green), [(c.name, c.detail) for c in green]
    assert [c.name for c in green] == [
        "verify_green",
        "ai_code_review_approved",
        "no_protected_paths",
        "no_frozen_paths",
        "diff_within_max",
        "no_human_rejection",
    ]
    # an unconfigured verify passes by design
    unconfigured = evaluate_code_gate(
        ai_verdict="approved",
        verify=None,
        diff_text=diff,
        human_rejected=False,
        protected_globs=None,
        frozen_globs=None,
        auto_max_diff_lines=400,
    )
    assert unconfigured[0].passed is True
    # so does an inapplicable one (info/prep stories)
    inapplicable = evaluate_code_gate(
        ai_verdict="approved",
        verify={"applicable": False, "passed": False},
        diff_text=diff,
        human_rejected=False,
        protected_globs=None,
        frozen_globs=None,
        auto_max_diff_lines=400,
    )
    assert inapplicable[0].passed is True
    # every check can fail at once
    red = evaluate_code_gate(
        ai_verdict="rejected",
        verify={"passed": False, "attempts": 2, "applicable": True},
        diff_text=diff,
        human_rejected=True,
        protected_globs="feature.py",
        frozen_globs="*.py",
        auto_max_diff_lines=1,
    )
    assert [c.name for c in red if not c.passed] == [
        "verify_green",
        "ai_code_review_approved",
        "no_protected_paths",
        "no_frozen_paths",
        "diff_within_max",
        "no_human_rejection",
    ]

# ---- malformed tool-call 400s are corrected in-round (e56b558d) -----------


def test_bad_tool_args_400_is_corrected_and_story_continues(tmp_path, monkeypatch):
    """The first coder answer for tool 'edit_file' is the 400; the second is
    a valid tool call. The story must continue past coding (reach review /
    human_review, not fail) and carry the note event naming the tool and the
    round."""
    gw = BadToolArgsGW(bad_rounds=1)
    with gate_client(tmp_path, monkeypatch, 'badargs-ok.db', gateway=gw) as (client, repo):
        # human spec gate + auto code gate: the coder must actually run
        project = make_project(client, repo, default_gate_code='auto')
        story = client.post(
            '/api/v1/stories',
            json={'title': 'g', 'project_id': project['id']},
        ).json()
        sid = story['id']
        client.post(f'/api/v1/stories/{sid}/queue')
        first = wait_status(client, sid, ('human_review', 'failed'))
        assert first['status'] == 'human_review', first.get('error')
        client.post(f'/api/v1/stories/{sid}/approve', json={'by': 't'})

        final = wait_status(client, sid, ('human_review', 'failed', 'done'))
        assert final['status'] != 'failed', final.get('error')
        notes = [n for n in note_events(detail(client, sid)) if n.get('bad_tool_args')]
        assert notes, 'a malformed tool-call correction must leave a note event'
        assert notes[0]['tool'] == 'edit_file'
        assert notes[0]['round'] >= 1
        assert notes[0]['feedback'] is False
        assert 'reason' not in notes[0]
        assert gw.four_hundreds == 1


def test_bad_tool_args_400_three_times_fails_the_story(tmp_path, monkeypatch):
    """Three bad-args 400s inside one round: the correction cap (2) is
    exhausted, the provider error re-raises and the story fails with the
    provider text in error."""
    gw = BadToolArgsGW(bad_rounds=3)
    with gate_client(tmp_path, monkeypatch, 'badargs-fail.db', gateway=gw) as (client, repo):
        project = make_project(client, repo, default_gate_code='auto')
        story = client.post(
            '/api/v1/stories',
            json={'title': 'g', 'project_id': project['id']},
        ).json()
        sid = story['id']
        client.post(f'/api/v1/stories/{sid}/queue')
        first = wait_status(client, sid, ('human_review', 'failed'))
        assert first['status'] == 'human_review', first.get('error')
        client.post(f'/api/v1/stories/{sid}/approve', json={'by': 't'})

        final = wait_status(client, sid, ('failed', 'human_review', 'done'))
        assert final['status'] == 'failed', final.get('error')
        assert 'Failed to parse tool call arguments' in (final.get('error') or '')
        assert 'edit_file' in (final.get('error') or '')
        # exactly the cap of corrections were attempted before giving up
        assert gw.four_hundreds == 3
        notes = [n for n in note_events(detail(client, sid)) if n.get('bad_tool_args')]
        assert len(notes) == 2
