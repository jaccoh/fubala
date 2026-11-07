"""The AI code review as a read-only, tool-using session with real evidence.

Phase A (inspect, model slot) -> phase B (changed tests + at most 2 mutations,
test slot, run by Fubala) -> phase C (verdict, model slot). The ledger, not the
reviewer, is the evidence; missing evidence or budget is `incomplete`, never
`approved`. In a cluster everything runs in the story's worker pod.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import MINIMAL_SPEC
from sqlmodel import select
from test_pod_executor import FakeKube
from test_proc_groups import _make_runner

from app import pods, procs, review_evidence, settings_store, workspaces
from app.db import session_scope
from app.models import Project, Story, StoryEvent, StoryGate, StoryNote, StoryStatus
from app.providers import Delta, ToolCall, Usage
from app.providers import claude_code as cc
from app.runner import _HeadlessRequeued

TOKEN = "sk-ant-oat01-review-test-token-0123456789"
CLAUDE_MODEL = "claude-max/claude-sonnet-5-5"
MUTATION = {
    "path": "calc.py",
    "line": 2,
    "original": "    return a + b",
    "replacement": "    return a - b",
    "tests": ["tests/test_calc.py"],
}


def _block(*entries: dict) -> str:
    return "MUTATIONS\n```json\n" + json.dumps(list(entries)) + "\n```"


REVIEW_A = "Looks right.\n\n" + _block(MUTATION)


# ---- unit: argv, toolbox, parsing, validation, decision --------------------


def _resolved(r):
    from app.registry import ProviderRegistry

    registry = ProviderRegistry(r.engine)
    registry.seed_if_empty()
    return registry.resolve(CLAUDE_MODEL)


def test_cli_session_argv_is_read_only(tmp_path):
    r = _make_runner(tmp_path)
    resolved = _resolved(r)
    argv, env, prompt = cc.build_cli_session(
        resolved, TOKEN, [{"role": "user", "content": "x"}],
        cli_tools=cc.READ_ONLY_CLI_TOOLS, max_turns=40,
    )
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    at = argv.index("--disallowedTools")
    assert argv[at + 1 : at + 9] == [
        "Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "WebFetch", "WebSearch", "Task",
    ]  # fmt: skip
    assert argv[argv.index("--max-turns") + 1] == "40"


def test_review_toolbox_has_no_write_or_run_tools(tmp_path):
    from app.agent_tools import ToolBox

    box = ToolBox(project_root=str(tmp_path), writable=False)
    names = {s["function"]["name"] for s in box.schemas()}
    assert names and not names & {"write_file", "edit_file", "run_command"}
    out = box.execute("write_file", json.dumps({"path": "x.py", "content": "boom"}))
    assert "error" in out and not (tmp_path / "x.py").exists()


def test_max_turns_result_is_reported():
    out = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": False})
    text, usage = cc.parse_cli_result("p", TOKEN, 0, out, "", allow_turn_cap=True)
    assert text == "" and usage.turn_cap
    with pytest.raises(cc.GatewayError):
        cc.parse_cli_result("p", TOKEN, 0, out, "")
    ok = json.dumps({"type": "result", "subtype": "success", "result": "fine"})
    assert not cc.parse_cli_result("p", TOKEN, 0, ok, "")[1].turn_cap


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("def test_x(): pass\n")
    return tmp_path


@pytest.mark.parametrize(
    "change",
    [
        {"path": "/etc/passwd"},
        {"path": "../escape.py"},
        {"path": "tests/test_calc.py", "original": "def test_x(): pass"},
        {"path": ".env"},
        {"original": "    return a * b"},
        {"replacement": "    return a + b"},
        {"replacement": "    x = 1\n    return a - b"},
        {"line": 99},
    ],
)
def test_mutation_refusals(tmp_path, change):
    wt = _tree(tmp_path)
    entry = {**MUTATION, **change}
    mutation, data, why = review_evidence.validate_mutation(entry, wt, ())
    assert mutation is None and data is None and why


def test_valid_mutation_replaces_exactly_one_line(tmp_path):
    wt = _tree(tmp_path)
    mutation, data, why = review_evidence.validate_mutation(MUTATION, wt, ())
    assert why is None and mutation is not None
    assert data == b"def add(a, b):\n    return a - b\n"


def test_diff_helpers():
    diff = (
        "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n@@\n+x\n"
        "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n"
        "+++ b/tests/test_a.py\n@@\n+x\n"
        "diff --git a/tests/test_gone.py b/tests/test_gone.py\n--- a/tests/test_gone.py\n"
        "+++ /dev/null\n@@\n-x\n"
        "diff --git a/web/a.spec.tsx b/web/a.spec.tsx\n--- /dev/null\n+++ b/web/a.spec.tsx\n+x\n"
    )
    assert review_evidence.changed_test_files(diff) == ["tests/test_a.py", "web/a.spec.tsx"]
    assert review_evidence.parse_mutations("no block") == (False, [])
    assert review_evidence.parse_mutations(REVIEW_A) == (True, [MUTATION])
    assert review_evidence.parse_mutations("MUTATIONS\nnone") == (True, [])


def _ledger(**kw) -> review_evidence.Ledger:
    run = review_evidence.TestRun("t", 0, ["tests/test_calc.py"])
    mut = review_evidence.MutationRun(
        review_evidence.Mutation("calc.py", 2, "a", "b"), "killed", 1
    )
    base = {"test_run": run, "mutations": [mut], "mutations_block": True}
    return review_evidence.Ledger(**{**base, **kw})


def test_decide():
    flags = review_evidence.SessionFlags()
    assert review_evidence.decide("approved", _ledger(), flags) == ("approved", None)
    assert review_evidence.decide("rejected", _ledger(test_run=None), flags)[0] == "rejected"
    for ledger in (
        _ledger(test_run=None),
        _ledger(mutations=[]),
        _ledger(mutations_block=False),
        _ledger(no_test_command=True),
        _ledger(over_budget=True),
        _ledger(pod_unavailable=True),
    ):
        assert review_evidence.decide("approved", ledger, flags)[0] == "incomplete"
    for bad in (
        review_evidence.SessionFlags(timed_out=True),
        review_evidence.SessionFlags(turn_cap=True),
    ):
        assert review_evidence.decide("approved", _ledger(), bad)[0] == "incomplete"


# ---- integration harness ----------------------------------------------------


class ReviewGW:
    """Tool-loop gateway: phase A offers tools, phase C answers a verdict."""

    def __init__(self, review_a: str = REVIEW_A, verdict: str = "VERDICT: APPROVED"):
        self.review_a, self.verdict = review_a, verdict
        self.calls: list[dict] = []
        self.tool_rounds = 0

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None, **kw):
        last = messages[-1]["content"]
        self.calls.append({"last": last, "tools": tools, "model": model})
        if last.startswith("=== REVIEW EVIDENCE"):
            yield Delta(text=self.verdict)
        elif messages[1]["content"].startswith("=== CODE REVIEW REQUEST"):
            if tools and self.tool_rounds:
                self.tool_rounds -= 1
                yield ToolCall(
                    id=f"c{self.tool_rounds}", name="list_files",
                    arguments=json.dumps({"path": f"dir{self.tool_rounds}"}),
                )
            else:
                yield Delta(text=self.review_a)
        else:
            yield Delta(text=self.verdict)
        yield Usage(prompt_tokens=1, completion_tokens=1)


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    }


def _setup(
    tmp_path, monkeypatch, *, gw=None, test_command=None, with_tests=True,
    gate=StoryGate.AUTO, **settings,
):
    """A story in ai_review with a worktree holding a coder diff."""
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    r = _make_runner(tmp_path)
    if gw is not None:
        r.gateway = gw
    settings_store.save_settings(r.engine, {"project_root": str(repo), **settings})
    wt, base, _ = workspaces.create(str(repo), "s1")
    (wt / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    # the project's test command: fails when the production line is changed
    (wt / "check.py").write_text(
        "import sys\nsys.exit(0 if 'return a + b' in open('calc.py').read() else 1)\n"
    )
    if with_tests:
        (wt / "tests").mkdir()
        (wt / "tests" / "test_calc.py").write_text("def test_add(): pass\n")
    command = test_command if test_command is not None else f"{sys.executable} check.py"
    with session_scope(r.engine) as db:
        db.add(Project(id="p1", name="demo", root=str(repo), test_command=command or None))
        db.add(
            Story(
                id="s1", title="add calc", status=StoryStatus.AI_REVIEW, spec=MINIMAL_SPEC,
                project_id="p1", model="fake/model", base_ref=base,
                gate_code=gate,
            )
        )
    r._running.add("s1")
    return r, wt


def _state(r):
    with session_scope(r.engine) as db:
        story = db.get(Story, "s1")
        notes = [
            json.loads(e.data)
            for e in db.exec(select(StoryEvent).where(StoryEvent.story_id == "s1")).all()
        ]
        return story.status, story.ai_verdict, story.ai_review or "", story.error, notes


def _gate_failed(notes) -> list[str]:
    gate = [n for n in notes if n.get("gate") == "code" and "checks" in n]
    assert gate, notes
    return [c["name"] for c in gate[-1]["checks"] if not c["passed"]]


def _incomplete(r, cause: str):
    status, verdict, review, _err, notes = _state(r)
    assert (status, verdict) == (StoryStatus.HUMAN_REVIEW, "incomplete")
    assert "## Review evidence" in review
    assert any(
        n.get("review_incomplete") and cause in n["reason"] for n in notes
    ), [n.get("reason") for n in notes if n.get("reason")]
    assert "ai_code_review_approved" in _gate_failed(notes)
    with session_scope(r.engine) as db:  # parked, never sent back to the coder
        assert db.get(Story, "s1").status == StoryStatus.HUMAN_REVIEW


def _run(r):
    asyncio.run(r._run_code_review("s1"))


# ---- tool-loop path ---------------------------------------------------------


def test_tool_loop_review_records_evidence_and_leaves_the_worktree_alone(
    tmp_path, monkeypatch
):
    gw = ReviewGW()
    r, wt = _setup(tmp_path, monkeypatch, gw=gw, gate=StoryGate.HUMAN)
    before = _hashes(wt)
    slots: list[tuple[bool, bool]] = []
    orig = type(r)._review_commands

    def spy(self, *a, **k):
        slots.append(("s1" in self._test_pool.holders(), "s1" in self._model_pool.holders()))
        return orig(self, *a, **k)

    monkeypatch.setattr(type(r), "_review_commands", spy)
    _run(r)
    status, verdict, review, _err, _notes = _state(r)
    assert (status, verdict) == (StoryStatus.HUMAN_REVIEW, "approved")
    assert "## Review evidence" in review
    assert "Changed tests: PASSED" in review and "Mutation 1: **killed**" in review
    assert "calc.py:2" in review
    assert _hashes(wt) == before  # the mutation ran in a scratch checkout
    assert slots == [(True, False)]  # a test slot and no model slot
    # phase A had tools; the verdict turn read the ledger
    assert gw.calls[0]["tools"] and "run_command" not in json.dumps(gw.calls[0]["tools"])
    assert "write_file" not in json.dumps(gw.calls[0]["tools"])
    assert gw.calls[1]["last"].startswith("=== REVIEW EVIDENCE")
    assert "Mutation 1: **killed**" in gw.calls[1]["last"]
    assert "## Changed files not shown" not in gw.calls[0]["last"]


def test_surviving_mutation_is_reported_and_still_approves(tmp_path, monkeypatch):
    survivor = {
        **MUTATION, "line": 1, "original": "def add(a, b):", "replacement": "def add(b, a):"
    }
    r, wt = _setup(
        tmp_path, monkeypatch, gw=ReviewGW(review_a="ok\n\n" + _block(survivor)),
        gate=StoryGate.HUMAN,
    )
    _run(r)
    status, verdict, review, _e, _n = _state(r)
    assert verdict == "approved" and "Mutation 1: **survived**" in review


def test_no_mutations_block_is_incomplete(tmp_path, monkeypatch):
    r, _ = _setup(tmp_path, monkeypatch, gw=ReviewGW(review_a="fine, no block"))
    _run(r)
    _incomplete(r, "no MUTATIONS block")


def test_every_mutation_refused_is_incomplete(tmp_path, monkeypatch):
    bad = {**MUTATION, "path": "../x.py"}
    r, _ = _setup(tmp_path, monkeypatch, gw=ReviewGW(review_a=_block(bad)))
    _run(r)
    _incomplete(r, "every mutation entry was refused")
    assert "Mutation refused" in _state(r)[2]


def test_approved_without_evidence_is_incomplete(tmp_path, monkeypatch):
    # no changed test file: nothing to run for the changed-test run
    r, _ = _setup(tmp_path, monkeypatch, gw=ReviewGW(), with_tests=False)
    _run(r)
    _incomplete(r, "approved without a completed changed-test run")


def test_no_test_command_is_incomplete(tmp_path, monkeypatch):
    r, _ = _setup(tmp_path, monkeypatch, gw=ReviewGW(), test_command="")
    _run(r)
    _incomplete(r, "no test command")


def test_rejection_stands_without_evidence(tmp_path, monkeypatch):
    gw = ReviewGW(review_a="bad", verdict="VERDICT: REJECTED - wrong")
    r, _ = _setup(tmp_path, monkeypatch, gw=gw)
    _run(r)
    status, verdict, _review, _e, _n = _state(r)
    assert verdict == "rejected" and status == StoryStatus.QUEUED


def test_tool_call_cap_is_incomplete(tmp_path, monkeypatch):
    gw = ReviewGW()
    gw.tool_rounds = 50
    r, _ = _setup(tmp_path, monkeypatch, gw=gw, review_max_tool_calls=5)
    _run(r)
    _incomplete(r, "turn / tool-call cap")


def test_phase_b_over_budget_is_incomplete(tmp_path, monkeypatch):
    r, _ = _setup(
        tmp_path, monkeypatch, gw=ReviewGW(),
        test_command="sleep 0.4; true #", review_test_budget_seconds=0.05,
    )
    _run(r)
    _incomplete(r, "time budget")


def test_a_changed_test_that_times_out_is_incomplete(tmp_path, monkeypatch):
    r, _ = _setup(tmp_path, monkeypatch, gw=ReviewGW(), test_command="exit 124 #")
    _run(r)
    _incomplete(r, "timed out")


# ---- CLI path in a worker pod ----------------------------------------------


def _result(text: str, subtype: str = "success") -> str:
    return json.dumps(
        {"type": "result", "subtype": subtype, "is_error": False, "result": text,
         "usage": {"input_tokens": 1, "output_tokens": 1}}
    )  # fmt: skip


class ScriptedKube(FakeKube):
    """Answers each exec by its script: CLI sessions from a queue, the changed
    tests and the mutations from `test_rc` / `mutation_rc`."""

    def __init__(self, sessions, *, test_rc=0, mutation_rc=1, **kw):
        super().__init__(**kw)
        self.sessions = list(sessions)
        self.test_rc, self.mutation_rc = test_rc, mutation_rc
        self.log: list[str] = []

    def exec(self, name, argv, stdin_bytes, on_output, timeout):
        script = argv[2] if argv[:2] == ["sh", "-c"] and len(argv) > 2 else ""
        if self.exec_error:
            raise self.exec_error
        self.execs.append((list(argv), stdin_bytes))
        if script == pods.HEADLESS_SCRIPT:
            self.log.append("session")
            rc, out = self.sessions.pop(0)
            return rc, out, ""
        if script == pods._MUTATE_SCRIPT:
            self.log.append("mutation")
            return self.mutation_rc, "tests ran\n", ""
        if "timeout --kill-after" in script:
            self.log.append("tests")
            return self.test_rc, "1 passed\n", ""
        return 0, "", ""


@pytest.fixture
def cluster(monkeypatch):
    monkeypatch.setenv("POD_NAME", "fubala-0")
    monkeypatch.setenv("POD_UID", "uid-123")
    monkeypatch.setenv("NODE_NAME", "stor01")
    monkeypatch.setenv("FUBALA_WORKER_IMAGE_REPO", "registry.example.com/fubala/fubala-worker")
    monkeypatch.setattr(pods, "preflight", lambda image: None)
    pods._WARNED.clear()
    pods._FALLBACK_HELD.clear()
    yield
    pods._FALLBACK_HELD.clear()
    pods._LIVE.clear()
    pods.set_kube_factory(None)


def _pod_setup(tmp_path, monkeypatch, kube, gate=StoryGate.AUTO, **kw):
    r, wt = _setup(
        tmp_path, monkeypatch, run_executor="pod", worker_image="t1", gate=gate, **kw
    )
    monkeypatch.setattr(pods, "_kube_factory", lambda: kube)
    resolved = _resolved(r)
    monkeypatch.setattr(type(r), "_claude_code_target", lambda self, model: (resolved, TOKEN))

    async def no_local_cli(*a, **k):
        raise AssertionError("a local claude CLI was spawned")

    def no_local_run(*a, **k):
        raise AssertionError("a local test process was spawned")

    monkeypatch.setattr(cc, "run_cli_agent", no_local_cli)
    monkeypatch.setattr(procs.LocalExecutor, "run", no_local_run)
    with session_scope(r.engine) as db:
        db.get(Story, "s1").model = CLAUDE_MODEL
    return r, wt


def test_pod_review_runs_sessions_tests_and_mutations_in_the_pod(
    tmp_path, monkeypatch, cluster
):
    kube = ScriptedKube([(0, _result(REVIEW_A)), (0, _result("VERDICT: APPROVED"))], node="dev1")
    r, wt = _pod_setup(tmp_path, monkeypatch, kube, gate=StoryGate.HUMAN)
    before = _hashes(wt)
    _run(r)
    status, verdict, review, _e, _n = _state(r)
    assert (status, verdict) == (StoryStatus.HUMAN_REVIEW, "approved"), review
    assert kube.log == ["session", "tests", "mutation", "session"]
    assert kube.created  # the review used the story's worker pod
    session = next(s for a, s in kube.execs if a[2:3] == [pods.HEADLESS_SCRIPT])
    assert b"Read,Grep,Glob" in session and b"--disallowedTools" in session
    test_exec = next(
        a for a, _ in kube.execs
        if "timeout --kill-after" in a[2]
        and a[2] not in (pods._MUTATE_SCRIPT, pods.HEADLESS_SCRIPT)
    )
    assert "tests/test_calc.py" in test_exec[-1]
    mut_exec, mut_stdin = next(
        (a, s) for a, s in kube.execs if a[2:3] == [pods._MUTATE_SCRIPT]
    )
    assert mut_exec[5] == "calc.py" and mut_stdin == b"def add(a, b):\n    return a - b\n"
    assert "Mutation 1: **killed**" in review and "Changed tests: PASSED" in review
    assert _hashes(wt) == before


def test_pod_session_timeout_and_max_turns_are_incomplete(tmp_path, monkeypatch, cluster):
    kube = ScriptedKube([(124, "")], node="dev1")
    r, _ = _pod_setup(tmp_path, monkeypatch, kube)
    _run(r)
    _incomplete(r, "timed out")
    assert kube.log == ["session"]  # no evidence phase after a dead session


def test_pod_max_turns_is_incomplete(tmp_path, monkeypatch, cluster):
    kube = ScriptedKube([(0, _result("", "error_max_turns"))], node="dev1")
    r, _ = _pod_setup(tmp_path, monkeypatch, kube)
    _run(r)
    _incomplete(r, "turn / tool-call cap")


def test_pod_mutation_timeout_is_incomplete(tmp_path, monkeypatch, cluster):
    kube = ScriptedKube(
        [(0, _result(REVIEW_A)), (0, _result("VERDICT: APPROVED"))], node="dev1", mutation_rc=124
    )
    r, _ = _pod_setup(tmp_path, monkeypatch, kube)
    _run(r)
    _incomplete(r, "timed out")


def test_pod_unavailable_requeues_once_then_incomplete_and_never_runs_locally(
    tmp_path, monkeypatch, cluster
):
    kube = ScriptedKube([], node="dev1")
    kube.exec_error = RuntimeError("api down")
    r, _ = _pod_setup(tmp_path, monkeypatch, kube)
    with pytest.raises(_HeadlessRequeued):
        _run(r)
    status, _v, _r, _e, notes = _state(r)
    assert status == StoryStatus.QUEUED
    assert any(n.get("review_pod_requeue") for n in notes)
    # the retry: back in ai_review, a fresh run
    with session_scope(r.engine) as db:
        db.get(Story, "s1").status = StoryStatus.AI_REVIEW
    r._discard_run("s1")
    r._running.add("s1")
    _run(r)
    _incomplete(r, "worker pod was unavailable twice")
    with session_scope(r.engine) as db:
        assert db.exec(select(StoryNote).where(StoryNote.story_id == "s1")).all()


# ---- the mutation exec script and the settings --------------------------------


@pytest.mark.skipif(
    not (shutil.which("setsid") and shutil.which("timeout")), reason="needs util-linux/coreutils"
)
@pytest.mark.parametrize(
    ("command", "rc"), [("grep -q 'a = 2' f.py", 0), ("grep -q 'a = 3' f.py", 1), ("sleep 5", 124)]
)
def test_mutation_script_writes_runs_and_restores(tmp_path, command, rc):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.py").write_text("a = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i")
    res = subprocess.run(
        ["sh", "-c", pods._MUTATE_SCRIPT, "_", str(repo), "f.py", command, "1", "1"],
        input=b"a = 2\n", capture_output=True, env={**os.environ, "HOME": str(tmp_path / "h")},
    )  # fmt: skip
    assert res.returncode == rc
    assert (repo / "f.py").read_text() == "a = 1\n"  # restored by the trap


def test_review_settings_ranges(client):
    body = client.get("/api/v1/settings").json()
    assert (
        body["review_max_tool_calls"],
        body["review_session_timeout_seconds"],
        body["review_test_budget_seconds"],
    ) == (40, 900, 600)
    ok = client.patch(
        "/api/v1/settings",
        json={"review_max_tool_calls": 5, "review_session_timeout_seconds": 3600,
              "review_test_budget_seconds": 60},
    )  # fmt: skip
    assert ok.status_code == 200 and ok.json()["review_max_tool_calls"] == 5
    for key, bad in (
        ("review_max_tool_calls", 4), ("review_max_tool_calls", 201),
        ("review_session_timeout_seconds", 59), ("review_session_timeout_seconds", 3601),
        ("review_test_budget_seconds", 59), ("review_test_budget_seconds", 3601),
    ):  # fmt: skip
        assert client.patch("/api/v1/settings", json={key: bad}).status_code == 422


# ---- caps, restore between mutations, runnable tests, missing pod -----------


def test_runnable_tests_filters_by_runner():
    mixed = ["tests/test_a.py", "web/a.test.ts", "web/b.spec.tsx", "x_test.py"]
    assert review_evidence.runnable_tests("uv run pytest -q", mixed) == [
        "tests/test_a.py", "x_test.py",
    ]
    assert review_evidence.runnable_tests("npx vitest run", mixed) == [
        "web/a.test.ts", "web/b.spec.tsx",
    ]
    assert review_evidence.runnable_tests("make test", mixed) == mixed


def test_mixed_diff_never_hands_a_ts_test_to_pytest(tmp_path, monkeypatch):
    r, wt = _setup(tmp_path, monkeypatch, gw=ReviewGW(), test_command="pytest")
    (wt / "web").mkdir()
    (wt / "web" / "calc.test.ts").write_text("test('x', () => {})\n")
    seen: list[str] = []

    def fake_run(self, command, stdin, env, timeout):
        seen.append(command)
        return 0, ""

    monkeypatch.setattr(procs.LocalExecutor, "run", fake_run)
    _run(r)
    assert seen and all("calc.test.ts" not in c for c in seen)
    assert "tests/test_calc.py" in seen[0]


def test_only_two_mutations_run_and_the_ledger_lists_two(tmp_path, monkeypatch):
    third = {**MUTATION, "line": 1, "original": "def add(a, b):", "replacement": "def add(b, a):"}
    second = {**MUTATION, "replacement": "    return a * b"}
    gw = ReviewGW(review_a="ok\n\n" + _block(MUTATION, second, third))
    r, _ = _setup(tmp_path, monkeypatch, gw=gw, gate=StoryGate.HUMAN)
    runs: list[str] = []
    orig = procs.LocalExecutor.run

    def spy(self, command, stdin, env, timeout):
        runs.append(command)
        return orig(self, command, stdin, env, timeout)

    monkeypatch.setattr(procs.LocalExecutor, "run", spy)
    _run(r)
    assert len(runs) == 3  # the changed-test baseline plus exactly two mutations
    review = _state(r)[2]
    assert "Mutation 1:" in review and "Mutation 2:" in review
    assert "Mutation 3:" not in review


def test_in_process_restores_the_scratch_file_between_mutations(tmp_path, monkeypatch):
    second = {**MUTATION, "replacement": "    return a * b"}
    gw = ReviewGW(review_a="ok\n\n" + _block(MUTATION, second))
    r, wt = _setup(tmp_path, monkeypatch, gw=gw, gate=StoryGate.HUMAN)
    contents: list[str] = []
    orig = procs.LocalExecutor.run

    def spy(self, command, stdin, env, timeout):
        contents.append((Path(self.cwd) / "calc.py").read_text())
        return orig(self, command, stdin, env, timeout)

    monkeypatch.setattr(procs.LocalExecutor, "run", spy)
    _run(r)
    assert _state(r)[1] == "approved"
    # changed-test baseline, mutation 1, mutation 2: each starts from the snapshot
    assert contents == [
        "def add(a, b):\n    return a + b\n",
        "def add(a, b):\n    return a - b\n",
        "def add(a, b):\n    return a * b\n",
    ]


def test_pod_mode_without_a_usable_executor_never_runs_locally(
    tmp_path, monkeypatch, cluster
):
    kube = ScriptedKube([], node="dev1")
    r, _ = _pod_setup(tmp_path, monkeypatch, kube)
    monkeypatch.delenv("FUBALA_WORKER_IMAGE_REPO")  # preflight fails: no executor
    with pytest.raises(_HeadlessRequeued):
        _run(r)
    assert _state(r)[0] == StoryStatus.QUEUED
    with session_scope(r.engine) as db:
        db.get(Story, "s1").status = StoryStatus.AI_REVIEW
    r._discard_run("s1")
    r._running.add("s1")
    _run(r)
    _incomplete(r, "worker pod was unavailable twice")
