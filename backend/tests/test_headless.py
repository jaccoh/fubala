"""Headless Claude Code CODE-executor: contract, outcomes, A/B, report.

All subprocess work goes through the fake `claude` script (see
headless_fake_claude.py): no network, no real CLI, no real provider key.
The pump/`zai` path is regression-covered by the rest of the suite.
"""

import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import MINIMAL_SPEC, FakeGateway

from app import headless as hx
from app.headless import HeadlessConfig

HERE = Path(__file__).parent
FAKE_SCRIPT = HERE / "headless_fake_claude.py"

# the key the fake provider carries; must appear in NO event/log/note
SECRET_KEY = "sk-zai-test-do-not-leak"


def install_fake_claude(
    tmp_path: Path, mode: str, *, env: dict[str, str] | None = None
) -> Path:
    """Put an executable `claude` on a fresh bindir; returns the bindir.

    The mode (and any extra env) is baked into the wrapper (not ambient env)
    so tests running in the same process can never see each other's mode —
    and, critically, because the headless child env is a CLOSED contract
    (PATH/HOME/TMPDIR + ANTHROPIC_* only): an ambient FAKE_* var would never
    reach the child, silently skipping the assertion it drives.
    """
    bindir = tmp_path / f"bin-{mode}-{len(env or {})}"
    bindir.mkdir(exist_ok=True)
    script = bindir / "claude"
    lines = [
        "#!/usr/bin/env python3",
        "import runpy, sys",
        "sys.argv[0] = 'claude'",
        f"__import__('os').environ['FAKE_CLAUDE_MODE'] = {mode!r}",
    ]
    for key, value in (env or {}).items():
        lines.append(f"__import__('os').environ[{key!r}] = {value!r}")
    lines.append(f"runpy.run_path({str(FAKE_SCRIPT)!r}, run_name='__main__')")
    script.write_text("\n".join(lines) + "\n")
    script.chmod(0o755)
    return bindir


class on_path:
    """`with on_path(bindir):` — bindir first on PATH for the block."""

    def __init__(self, bindir: Path):
        self.bindir = bindir
        self._mp = pytest.MonkeyPatch()

    def __enter__(self):
        self._mp.setenv("PATH", f"{self.bindir}{os.pathsep}{os.environ.get('PATH', '')}")
        return self._mp

    def __exit__(self, *exc):
        self._mp.undo()


def make_config(**kw) -> HeadlessConfig:
    base = dict(
        base_url="https://api.z.ai/api/anthropic",
        api_key=SECRET_KEY,
        model="zai/glm-5.3",
        max_turns=40,
        timeout_seconds=30.0,
    )
    base.update(kw)
    return HeadlessConfig(**base)


def _init_repo(tmp_path: Path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("# demo\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return str(repo)


def _wait_for_status(client, story_id: str, want, timeout: float = 90.0) -> dict:
    wants = {want} if isinstance(want, str) else set(want)
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = client.get(f"/api/v1/stories/{story_id}").json()
        if last.get("status") in wants:
            return last
        time.sleep(0.1)
    pytest.fail(
        f"story never reached {want}; last: "
        f"{last and last.get('status')} {last and last.get('error')}"
    )


def _events(client, story_id: str, kind: str | None = None) -> list[dict]:
    detail = client.get(f"/api/v1/stories/{story_id}").json()
    out = []
    for e in detail.get("events", []):
        if kind and e.get("kind") != kind:
            continue
        try:
            data = json.loads(e.get("data") or "{}")
        except ValueError:
            data = {}
        out.append({"kind": e.get("kind"), "data": data, "raw": e.get("data") or ""})
    return out


def _notes(client, story_id: str) -> list[dict]:
    detail = client.get(f"/api/v1/stories/{story_id}").json()
    return detail.get("notes", [])


def _seed_provider(client, key: str = SECRET_KEY) -> str:
    """Key the seeded zai provider and add glm-5.3; returns canonical id."""
    res = client.post("/api/v1/providers/zai/key", json={"key": key})
    assert res.status_code in (200, 201, 204), res.text
    res = client.post("/api/v1/providers/zai/models", json={"model_id": "glm-5.3"})
    assert res.status_code in (200, 201, 409), res.text
    return "zai/glm-5.3"


def _no_key_anywhere(client, story_id: str, key: str = SECRET_KEY) -> None:
    detail = client.get(f"/api/v1/stories/{story_id}").json()
    assert key not in json.dumps(detail), "provider key leaked into the story payload"
    for e in _events(client, story_id):
        assert key not in e["raw"], "provider key leaked into an event"
    for n in _notes(client, story_id):
        assert key not in json.dumps(n), "provider key leaked into a note"


# ---- unit: child env, prompt, mapping -------------------------------------


def test_child_env_is_the_closed_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("FUBALA_DATABASE_PATH", "/tmp/should-not-ride-along.db")
    monkeypatch.setenv("ZAI_API_KEY", SECRET_KEY)
    bindir = install_fake_claude(tmp_path, "ok")
    with on_path(bindir):
        child = hx._child_env(make_config())
    assert child["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
    assert child["ANTHROPIC_AUTH_TOKEN"] == SECRET_KEY
    assert child["ANTHROPIC_MODEL"] == "glm-5.3"
    assert child["ANTHROPIC_SMALL_FAST_MODEL"].endswith("flash")
    assert child["API_TIMEOUT_MS"] == "3000000" == str(hx.API_TIMEOUT_MS)
    assert child["DISABLE_TELEMETRY"] == "1"
    assert child["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    # closed contract: nothing else from the parent rides along
    assert "FUBALA_DATABASE_PATH" not in child
    assert "ZAI_API_KEY" not in child
    assert {"PATH", "HOME"} <= set(child)


def test_timeout_bounds_and_api_timeout_ordering():
    assert hx.HEADLESS_TIMEOUT_DEFAULT == 2400.0
    assert hx.HEADLESS_TIMEOUT_MIN == 30.0
    assert hx.HEADLESS_TIMEOUT_MAX == 7200.0
    # spec-pinned values; our killpg bounds the child regardless
    assert hx.API_TIMEOUT_MS == 3000000


def test_flash_model_resolution():
    assert hx.flash_model_for(make_config()) in ("glm-flash", "flash")
    assert hx.flash_model_for(make_config(small_fast_model="zai/glm-flash")) == "glm-flash"
    assert hx.flash_model_for(make_config(model="glm-5.3")) == "flash"
    assert hx.strip_provider_prefix("zai/glm-5.3") == "glm-5.3"
    assert hx.strip_provider_prefix("glm-5.3") == "glm-5.3"


def test_prompt_shape_and_tool_neutral_rules():
    prompt = hx.build_prompt(
        title="Add the thing",
        spec="## Goal\nDo it.",
        feedback="tests were missing",
        human_reasons=["no error handling"],
        test_command="sh tools/test.sh",
        verify_command="make test",
    )
    assert prompt.startswith("=== CODE REQUEST ===")
    assert "# Story: Add the thing" in prompt
    assert "## Spec to implement" in prompt
    assert "## Previous attempt feedback (fix this too)" in prompt
    assert "tests were missing" in prompt
    assert "no error handling" in prompt
    assert "sh tools/test.sh" in prompt
    assert "make test" in prompt
    # tool-neutral rules, verbatim in spirit
    for phrase in (
        "Do not commit",
        "full verify",
        "scratch files",
        "test command",
    ):
        assert phrase in prompt, f"missing rule phrase: {phrase}"


def test_ok_outcome(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    with on_path(install_fake_claude(tmp_path, "ok")):
        out = hx.run_code_stage(story_id="s1", worktree=wt, prompt="go", config=make_config())
    assert out.outcome == "ok"
    assert out.num_turns == 3
    assert isinstance(out.duration_ms, int) and out.duration_ms >= 0
    assert out.usage["input_tokens"] == 120
    assert out.usage["cache_read_input_tokens"] == 200
    assert "feature.py" in (out.summary or "")
    assert (wt / "feature.py").exists()
    assert SECRET_KEY not in (out.summary or "")


def test_delivered_prompt_carries_the_tool_neutral_rules(tmp_path, monkeypatch):
    """The blocker: the runner must actually DELIVER the rules, and a test
    must assert the prompt that reaches the CLI's argv — not only that
    `build_prompt` can build one."""
    # a pump-shaped CODE REQUEST: TESTS block (tool-specific) + spec
    pump_prompt = "\n".join(
        [
            "=== CODE REQUEST ===",
            "# Story: Add feature",
            "## Spec to implement",
            "## Goal\nDo it.",
            "=== TESTS ===",
            "This project's test command is: `make test`",
            "Run it with the run_tests tool (no permission request needed); pass "
            "`paths` (e.g. ['tests/test_x.py']) to run individual files, omit it "
            "for the full suite.",
        ]
    )
    delivered = hx.with_rules(pump_prompt, test_command="make test", verify_command="make verify")
    # the pump-only tool instructions are gone; only tool-neutral rules stay
    assert "run_tests tool" not in delivered
    assert "=== TESTS ===" not in delivered
    assert "make test" in delivered
    assert "make verify" in delivered
    assert "sh tools/test.sh <paths>" in delivered
    for phrase in (
        "Do not commit",
        "full verify",
        "scratch files",
    ):
        assert phrase in delivered
    # the CODE REQUEST body survives untouched
    assert delivered.startswith("=== CODE REQUEST ===")
    assert "## Spec to implement" in delivered
    assert "## Goal\nDo it." in delivered
    # idempotent: a second pass must not duplicate the rules
    assert hx.with_rules(delivered, test_command="make test") == delivered
    assert delivered.count("## Rules") == 1


def test_delivered_prompt_in_child_argv(tmp_path, monkeypatch):
    """End-to-end: the prompt the CLI receives (its argv) carries the rules —
    proves the runner->headless wiring, not just the helper."""
    wt = tmp_path / "wt"
    wt.mkdir()
    capture = tmp_path / "argv.txt"
    bindir = install_fake_claude(
        tmp_path, "capture", env={"FAKE_CLAUDE_CAPTURE": str(capture)}
    )
    with on_path(bindir):
        out = hx.run_code_stage(
            story_id="argv1",
            worktree=wt,
            prompt=hx.with_rules(
                "=== CODE REQUEST ===\n## Spec to implement\n## Goal\nDo it.",
                test_command="make test",
                verify_command="make verify",
            ),
            config=make_config(),
        )
    assert out.outcome == "ok"
    argv = capture.read_text()
    assert "sh tools/test.sh <paths>" in argv
    assert "Do not commit" in argv
    assert "--max-turns" in argv and "bypassPermissions" in argv
    # the child never sees the pump's tool instructions
    assert "run_tests tool" not in argv


def test_crash_outcome(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    with on_path(install_fake_claude(tmp_path, "crash")):
        out = hx.run_code_stage(story_id="s2", worktree=wt, prompt="go", config=make_config())
    assert out.outcome == "fix"
    assert out.exit_code == 3
    assert "claude crashed while planning edits" in (out.reason or "")


def test_rate_limit_is_transient(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    with on_path(install_fake_claude(tmp_path, "ratelimit")):
        out = hx.run_code_stage(story_id="s3", worktree=wt, prompt="go", config=make_config())
    assert out.outcome == "transient"
    assert out.exit_code == 1
    assert "rate limit" in (out.reason or "")


# the recorded stdout line of the 26 Oct incident: exit 1, empty stderr, the
# 429/usage limit living only in an isApiErrorMessage stream event. z.ai
# states the reset in its own zone (UTC+8): 19:29:59 there = 11:29:59Z.
RECORDED_STREAM_LIMIT_TEXT = (
    "API Error: 429 "
    '{"type":"error","error":{"type":"rate_limit_error","code":"1308",'
    '"message":"[1308][Usage limit reached for 5 hour. '
    'Your limit will reset at 2026-10-26 19:29:59]"}}'
)
RECORDED_STREAM_LIMIT_LINE = json.dumps(
    {
        "type": "assistant",
        "isApiErrorMessage": True,
        "message": {"content": [{"type": "text", "text": RECORDED_STREAM_LIMIT_TEXT}]},
    }
)


def test_stream_usage_limit_line_is_transient_with_reset(tmp_path):
    """Feeding the exact recorded line: the run is transient (never a fix),
    the reset is read in the provider's zone and the reason names the limit."""
    wt = tmp_path / "wt"
    wt.mkdir()
    bindir = install_fake_claude(
        tmp_path,
        "streamlimit",
        env={"FAKE_CLAUDE_RESET_AT": "2026-10-26 19:29:59"},
    )
    with on_path(bindir):
        out = hx.run_code_stage(
            story_id="s-sl",
            worktree=wt,
            prompt="go",
            config=make_config(limit_reset_tz="Asia/Shanghai"),
        )
    assert out.outcome == "transient"
    assert out.exit_code == 1
    assert out.limit_note == "Usage limit reached for 5 hour"
    assert out.limit_reset_at == datetime(2026, 10, 26, 11, 29, 59, tzinfo=UTC)
    assert "rate limit" in (out.reason or "")
    assert "Usage limit reached for 5 hour" in (out.reason or "")
    assert "11:29:59" in (out.reason or "")


def test_stream_rate_limit_needs_the_error_flag_and_a_marker():
    # ordinary assistant text quoting an API error: never a requeue
    quoted = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "the API said 429 rate_limit_error once"}
            ]
        },
    }
    # flagged as an API error but no limit marker
    flagged_plain = {
        "type": "assistant",
        "isApiErrorMessage": True,
        "message": {"content": [{"type": "text", "text": "overloaded provider"}]},
    }
    assert hx.stream_rate_limit([quoted]) is None
    assert hx.stream_rate_limit([flagged_plain]) is None
    real = json.loads(RECORDED_STREAM_LIMIT_LINE)
    note, reset = hx.stream_rate_limit([real], tz="Asia/Shanghai")
    assert note == "Usage limit reached for 5 hour"
    assert reset == datetime(2026, 10, 26, 11, 29, 59, tzinfo=UTC)


def test_silent_exit1_is_a_fix(tmp_path):
    """Exit 1 with no stream evidence and no stderr fails as today — the
    stream-limit requeue must not swallow real failures."""
    wt = tmp_path / "wt"
    wt.mkdir()
    with on_path(install_fake_claude(tmp_path, "exit1")):
        out = hx.run_code_stage(story_id="s-e1", worktree=wt, prompt="go", config=make_config())
    assert out.outcome == "fix"
    assert out.exit_code == 1
    assert out.reason == "headless exit 1: (no stderr)"


def test_max_turns_cap_is_failure(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    with on_path(install_fake_claude(tmp_path, "turns")):
        out = hx.run_code_stage(
            story_id="s4", worktree=wt, prompt="go", config=make_config(max_turns=5)
        )
    assert out.outcome == "fix"
    assert out.num_turns == 5
    assert "max-turns" in (out.reason or "")


def test_missing_binary_is_a_fix(tmp_path, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    # on_path only PREPENDS the dir, and the image now ships a real CLI
    # (/opt/claude-code/bin/claude): a real binary further down PATH would be
    # launched and hang until the headless timeout. A PATH with nothing on
    # it is the only faithful "binary missing" world.
    monkeypatch.setenv("PATH", str(empty))
    out = hx.run_code_stage(story_id="s5", worktree=tmp_path, prompt="go", config=make_config())
    assert out.outcome == "fix"
    assert "not found" in (out.reason or "")


def test_timeout_kills_process_group(tmp_path):
    marker = tmp_path / "child.pid"
    wt = tmp_path / "wt"
    wt.mkdir()
    started = time.monotonic()
    bindir = install_fake_claude(
        tmp_path, "sleep", env={"FAKE_CLAUDE_SLEEP_MARKER": str(marker)}
    )
    with on_path(bindir):
        out = hx.run_code_stage(
            story_id="s6",
            worktree=wt,
            prompt="go",
            config=make_config(timeout_seconds=1.5),
        )
    assert out.outcome == "fix"
    assert out.timed_out is True
    assert out.reason == "headless timeout"
    # not a wait: upper bound proving the 1.5s headless timeout fired promptly
    assert time.monotonic() - started < 30, "killpg must reap the group promptly"

    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    if marker.exists():
        child_pid = int(marker.read_text().strip())
        # not a pipeline wait: grace window for the OS to reap the killed child
        deadline = time.monotonic() + 10
        while alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not alive(child_pid), "the child survived killpg"


def test_echoed_key_is_redacted_from_the_final_text(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    with on_path(install_fake_claude(tmp_path, "echokey")):
        out = hx.run_code_stage(story_id="s7", worktree=wt, prompt="go", config=make_config())
    assert out.outcome == "ok"
    assert SECRET_KEY not in (out.summary or "")
    assert "[REDACTED" in (out.summary or "")


def test_config_dir_is_fresh_and_removed(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    bindir = install_fake_claude(tmp_path, "ok")
    seen: list[str] = []
    real_popen = subprocess.Popen

    def spy(argv, **kw):
        seen.append(kw["env"]["CLAUDE_CONFIG_DIR"])
        return real_popen(argv, **kw)

    with on_path(bindir) as mp:
        mp.setattr(hx.subprocess, "Popen", spy)
        hx.run_code_stage(story_id="s8", worktree=wt, prompt="go", config=make_config())
        hx.run_code_stage(story_id="s9", worktree=wt, prompt="go", config=make_config())
    assert len(seen) == 2 and seen[0] != seen[1], "config dir must be fresh per run"
    for d in seen:
        assert not Path(d).exists(), "config dir must be removed after the run"
    assert all(d.startswith(str(wt)) is False for d in seen)


# ---- live watch / rework: stream relay, transcript, never-ask, denials ----


def test_stream_events_are_relayed_in_order(tmp_path):
    """LIVE WATCH: each stream line becomes a relay payload, in order."""
    wt = tmp_path / "wt"
    wt.mkdir()
    seen: list[dict] = []
    with on_path(install_fake_claude(tmp_path, "ok")):
        out = hx.run_code_stage(
            story_id="s10",
            worktree=wt,
            prompt="go",
            config=make_config(),
            on_event=seen.append,
        )
    assert out.outcome == "ok"
    kinds = [(e.get("type"), e.get("tool")) for e in seen]
    # assistant text -> tool_use -> tool_result -> assistant text, in order
    assert kinds == [
        ("assistant", None),
        ("tool_use", "Write"),
        ("tool_result", None),
        ("assistant", None),
    ]
    assert "Implementing the spec" in seen[0]["text"]
    assert seen[1]["input"] == "feature.py"
    assert seen[2]["status"] == "ok" and seen[2]["chars"] == len("wrote 20 bytes")
    # num_turns / usage / duration come from the stream's final result event
    assert out.num_turns == 3
    assert out.usage["input_tokens"] == 120
    assert out.usage["cache_read_input_tokens"] == 200
    assert isinstance(out.duration_ms, int)


def test_stream_relay_redacts_the_key(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    seen: list[dict] = []
    with on_path(install_fake_claude(tmp_path, "echokey")):
        out = hx.run_code_stage(
            story_id="s11",
            worktree=wt,
            prompt="go",
            config=make_config(),
            on_event=seen.append,
        )
    assert out.outcome == "ok"
    assert seen and seen[0]["type"] == "assistant"
    assert SECRET_KEY not in json.dumps(seen), "key leaked into a relayed stream event"
    assert SECRET_KEY not in (out.summary or "")


def test_missing_result_event_is_a_failure(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    with on_path(install_fake_claude(tmp_path, "noresult")):
        out = hx.run_code_stage(story_id="s12", worktree=wt, prompt="go", config=make_config())
    assert out.outcome == "fix"
    assert "without a result event" in (out.reason or "")


def test_transcript_is_copied_and_redacted(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    artifacts = tmp_path / "artifacts"
    with on_path(install_fake_claude(tmp_path, "ok")):
        out = hx.run_code_stage(
            story_id="s13",
            worktree=wt,
            prompt="go",
            config=make_config(artifacts_dir=str(artifacts)),
        )
    assert out.outcome == "ok"
    assert out.transcript_path
    kept = Path(out.transcript_path)
    assert kept.is_file(), "the transcript artifact must survive the run"
    assert kept.parent == artifacts / "headless" / "s13"
    body = kept.read_text()
    assert '"type": "user"' in body
    # the fake's transcript embeds the auth token; the kept copy must not
    assert SECRET_KEY not in body, "key leaked into the kept transcript"
    assert "[REDACTED" in body


def test_transcript_survives_a_crash(tmp_path):
    """Even a failing run keeps whatever transcript the child managed to
    write — that is exactly the run a human wants to inspect."""
    wt = tmp_path / "wt"
    wt.mkdir()
    artifacts = tmp_path / "artifacts"
    with on_path(install_fake_claude(tmp_path, "crash")):
        out = hx.run_code_stage(
            story_id="s14",
            worktree=wt,
            prompt="go",
            config=make_config(artifacts_dir=str(artifacts)),
        )
    assert out.outcome == "fix"
    assert out.transcript_path and Path(out.transcript_path).is_file()


def test_disallowed_tools_argument_is_present(tmp_path):
    """NEVER ASK: the child must never reach for AskUserQuestion."""
    wt = tmp_path / "wt"
    wt.mkdir()
    capture = tmp_path / "argv.txt"
    bindir = install_fake_claude(
        tmp_path, "capture", env={"FAKE_CLAUDE_CAPTURE": str(capture)}
    )
    with on_path(bindir):
        out = hx.run_code_stage(
            story_id="s15", worktree=wt, prompt="go", config=make_config()
        )
    assert out.outcome == "ok"
    argv = capture.read_text().split("\n")
    i = argv.index("--disallowedTools")
    assert argv[i + 1] == "AskUserQuestion"
    # the stream flags are the live-watch contract
    j = argv.index("--output-format")
    assert argv[j + 1] == "stream-json"
    assert "--verbose" in argv


def test_prompt_says_never_ask(tmp_path):
    prompt = hx.build_prompt(title="t", spec="s")
    assert "Never ask questions" in prompt
    delivered = hx.with_rules("=== CODE REQUEST ===\n## Spec to implement\nx")
    assert "Never ask questions" in delivered
    assert "state your assumptions" in delivered


def test_permission_denials_surface_on_the_outcome(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    with on_path(install_fake_claude(tmp_path, "denials")):
        out = hx.run_code_stage(story_id="s16", worktree=wt, prompt="go", config=make_config())
    assert out.outcome == "ok"
    assert out.permission_denials == [{"tool": "Bash", "input": "rm -rf /"}]


def test_timeout_bounded_on_a_silent_child(tmp_path):
    """A child that emits nothing and never exits is still bounded by
    headless_timeout_seconds (the read loop owns the deadline)."""
    wt = tmp_path / "wt"
    wt.mkdir()
    started = time.monotonic()
    with on_path(install_fake_claude(tmp_path, "sleep")):
        out = hx.run_code_stage(
            story_id="s17", worktree=wt, prompt="go", config=make_config(timeout_seconds=2.0)
        )
    assert out.outcome == "fix" and out.timed_out is True
    assert out.reason == "headless timeout"
    # not a wait: upper bound proving the 2s headless timeout fired promptly
    assert time.monotonic() - started < 30


# ---- A/B assignment -------------------------------------------------------


def test_ab_assignment_is_deterministic_and_bounded():
    from app.models import headless_assignment

    for sid in ("a", "b", "story-123", "0" * 32):
        assert headless_assignment(sid, 0.5) == headless_assignment(sid, 0.5)
    assert headless_assignment("x", 0.0) is False
    assert headless_assignment("x", 1.0) is True
    assert headless_assignment("x", -1.0) is False
    # a 0.5 share splits a population roughly in half
    hits = sum(
        1 for i in range(200) if headless_assignment(f"story-{i:04d}", 0.5)
    )
    assert 60 <= hits <= 140, f"0.5 share produced {hits}/200 headless stories"


def test_ab_share_validation_and_default(client, tmp_path):
    assert client.patch(
        "/api/v1/settings", json={"headless_timeout_seconds": 10}
    ).status_code == 422
    assert client.patch(
        "/api/v1/settings", json={"headless_timeout_seconds": 8000}
    ).status_code == 422
    ok = client.patch("/api/v1/settings", json={"headless_timeout_seconds": 1800})
    assert ok.status_code == 200, ok.text
    assert ok.json()["headless_timeout_seconds"] == 1800
    # default 2400 comes back from a fresh app
    settings = client.get("/api/v1/settings").json()
    assert settings["headless_timeout_seconds"] == 1800


def test_headless_timeout_default_is_2400(client):
    assert client.get("/api/v1/settings").json()["headless_timeout_seconds"] == 2400.0


def test_ab_share_api_bounds_and_zero_default(tmp_path, monkeypatch, gateway):
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "ab.db"))
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(gateway=gateway)) as client:
        client.patch("/api/v1/settings", json={"story_poll_interval": 60})
        proj = client.post(
            "/api/v1/projects", json={"name": "ab", "root": _init_repo(tmp_path)}
        ).json()
        assert proj["ab_headless_share"] == 0.0
        assert client.patch(
            f"/api/v1/projects/{proj['id']}", json={"ab_headless_share": 1.5}
        ).status_code == 422
        assert client.patch(
            f"/api/v1/projects/{proj['id']}", json={"ab_headless_share": -0.1}
        ).status_code == 422
        res = client.patch(
            f"/api/v1/projects/{proj['id']}", json={"ab_headless_share": 0.5}
        )
        assert res.status_code == 200, res.text
        assert res.json()["ab_headless_share"] == 0.5


def test_ab_assignment_materializes_on_queue(tmp_path, monkeypatch, gateway):
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "abq.db"))
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(gateway=gateway)) as client:
        client.patch("/api/v1/settings", json={"story_poll_interval": 60})
        proj = client.post(
            "/api/v1/projects", json={"name": "ab", "root": _init_repo(tmp_path)}
        ).json()
        client.patch(f"/api/v1/projects/{proj['id']}", json={"ab_headless_share": 1.0})

        story = client.post(
            "/api/v1/stories",
            json={"title": "routed", "project_id": proj["id"]},
        ).json()
        assert story["execution_override"] is None
        assert client.post(f"/api/v1/stories/{story['id']}/queue").status_code == 200
        after = client.get(f"/api/v1/stories/{story['id']}").json()
        assert after["execution"] == "headless"
        assert after["execution_override"] == "headless"
        # an explicit override is never re-rolled
        explicit = client.post(
            "/api/v1/stories",
            json={
                "title": "pinned",
                "project_id": proj["id"],
                "execution": "internal",
            },
        ).json()
        client.post(f"/api/v1/stories/{explicit['id']}/queue")
        pinned = client.get(f"/api/v1/stories/{explicit['id']}").json()
        assert pinned["execution"] == "internal"
        # share 0 never assigns
        client.patch(f"/api/v1/projects/{proj['id']}", json={"ab_headless_share": 0.0})
        plain = client.post(
            "/api/v1/stories",
            json={"title": "never", "project_id": proj["id"]},
        ).json()
        client.post(f"/api/v1/stories/{plain['id']}/queue")
        assert client.get(f"/api/v1/stories/{plain['id']}").json()["execution"] == "internal"


def test_ab_assignment_survives_restart(tmp_path, monkeypatch, gateway):
    from fastapi.testclient import TestClient

    from app.main import create_app

    db = str(tmp_path / "restart.db")
    monkeypatch.setenv("FUBALA_DATABASE_PATH", db)
    with TestClient(create_app(gateway=gateway)) as client:
        client.patch("/api/v1/settings", json={"story_poll_interval": 60})
        proj = client.post(
            "/api/v1/projects", json={"name": "ab", "root": _init_repo(tmp_path)}
        ).json()
        client.patch(f"/api/v1/projects/{proj['id']}", json={"ab_headless_share": 0.5})
        ids = [
            client.post(
                "/api/v1/stories", json={"title": f"s{i}", "project_id": proj["id"]}
            ).json()["id"]
            for i in range(12)
        ]
        for sid in ids:
            client.post(f"/api/v1/stories/{sid}/queue")
        first = {sid: client.get(f"/api/v1/stories/{sid}").json()["execution"] for sid in ids}

    # a second process boots against the same DB
    monkeypatch.setenv("FUBALA_DATABASE_PATH", db)
    with TestClient(create_app(gateway=gateway)) as client:
        second = {sid: client.get(f"/api/v1/stories/{sid}").json()["execution"] for sid in ids}
    assert first == second, "A/B assignment must be stable across restarts"
    assert any(v == "headless" for v in first.values()), "expected some headless stories"
    assert any(v == "internal" for v in first.values()), "expected some pump stories"


def test_ab_draw_thousand_ids_bounded_and_repeatable():
    """1000 synthetic ids at share 0.5: the split stays within ±100 of the
    target and an identical re-run resolves identically (deterministic)."""
    from app.models import headless_assignment

    ids = [f"ab-synthetic-{i:05d}" for i in range(1000)]
    first = [sid for sid in ids if headless_assignment(sid, 0.5)]
    assert 400 <= len(first) <= 600, f"0.5 share produced {len(first)}/1000 headless"
    second = [sid for sid in ids if headless_assignment(sid, 0.5)]
    assert first == second, "the draw must be deterministic per story id"


def _queued_at_zero_share(client, tmp_path: Path, **story_body) -> tuple[dict, dict]:
    """Project + story queued while the share is still 0.0, then share 1.0.

    Returns (story, project). The share is armed only AFTER the queue call,
    so the queue-time draw passed the story over and only the scheduler's
    pickup draw can materialize it — the exact path reject/re-spec/release/
    rate-limit requeues take.
    """
    proj = client.post(
        "/api/v1/projects", json={"name": "ab", "root": _init_repo(tmp_path)}
    ).json()
    story = client.post(
        "/api/v1/stories", json={"title": "pickup", "project_id": proj["id"], **story_body}
    ).json()
    assert client.post(f"/api/v1/stories/{story['id']}/queue").status_code == 200
    res = client.patch(f"/api/v1/projects/{proj['id']}", json={"ab_headless_share": 1.0})
    assert res.status_code == 200, res.text
    return story, proj


def _ab_notes(client, story_id: str) -> list[dict]:
    return [
        e["data"]
        for e in _events(client, story_id, "note")
        if "ab_assignment" in e["data"]
    ]


def test_ab_pickup_draws_when_queue_time_missed(tmp_path, monkeypatch, gateway, client):
    """Share 1.0 + one scheduler tick routes a NULL-execution queued story
    headless, with the ab note. Scheduler HARD-OFF first and `_tick` called
    directly: no polling, the tick alone owns the claim."""
    # scheduler HARD-off: stop() awaits the cancelled loop task, so no tick
    # can fire between the queue call and the direct one below
    client.portal.call(client.app.state.story_runner.stop)
    story, _proj = _queued_at_zero_share(client, tmp_path)

    client.portal.call(client.app.state.story_runner._tick, 1)

    after = client.get(f"/api/v1/stories/{story['id']}").json()
    assert after["execution_override"] == "headless"
    assert after["execution"] == "headless"
    notes = _ab_notes(client, story["id"])
    assert len(notes) == 1, "exactly one ab note, and never a re-roll"
    assert notes[0]["ab_assignment"] == "headless"
    assert notes[0]["share"] == 1.0
    assert notes[0]["feedback"] is False, "must stay out of the coder feedback"
    assert notes[0]["text"] == "A/B: headless (share 1.0)"


def test_ab_pickup_share_zero_changes_nothing(tmp_path, monkeypatch, gateway, client):
    """Share 0.0: the tick still picks the story up, but never materializes
    an execution and leaves no ab note."""
    client.portal.call(client.app.state.story_runner.stop)
    proj = client.post(
        "/api/v1/projects", json={"name": "ab", "root": _init_repo(tmp_path)}
    ).json()
    story = client.post(
        "/api/v1/stories", json={"title": "plain", "project_id": proj["id"]}
    ).json()
    assert client.post(f"/api/v1/stories/{story['id']}/queue").status_code == 200

    client.portal.call(client.app.state.story_runner._tick, 1)

    after = client.get(f"/api/v1/stories/{story['id']}").json()
    assert after["execution_override"] is None
    assert _ab_notes(client, story["id"]) == []


def test_ab_pickup_leaves_pinned_internal_alone(tmp_path, monkeypatch, gateway, client):
    """An operator-pinned `execution="internal"` survives a tick at share
    1.0: pins always win over the project's share."""
    client.portal.call(client.app.state.story_runner.stop)
    story, _proj = _queued_at_zero_share(client, tmp_path, execution="internal")

    client.portal.call(client.app.state.story_runner._tick, 1)

    after = client.get(f"/api/v1/stories/{story['id']}").json()
    assert after["execution_override"] == "internal"
    assert _ab_notes(client, story["id"]) == []


# ---- integration: the pipeline around the fake CLI ------------------------


def test_runner_delivers_rules_in_the_real_prompt(tmp_path, monkeypatch, gateway):
    """The rules must reach the CLI the runner spawns (argv), not just exist
    in a helper. Runs the app end-to-end with a capturing fake CLI."""
    capture = tmp_path / "argv.txt"
    client = _headless_client(
        tmp_path,
        monkeypatch,
        gateway,
        "capture",
        fake_env={"FAKE_CLAUDE_CAPTURE": str(capture)},
    )
    try:
        model = _seed_provider(client)
        proj = client.post(
            "/api/v1/projects",
            json={"name": "demo", "root": _init_repo(tmp_path / "repo-dir")},
        ).json()
        story = client.post(
            "/api/v1/stories",
            json={
                "title": "Prompted",
                "project_id": proj["id"],
                "execution": "headless",
                "model": model,
            },
        ).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        _wait_for_status(client, story["id"], "human_review", 60)
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not capture.exists():
            time.sleep(0.1)
        assert capture.exists(), "the fake CLI was never invoked"
        delivered = capture.read_text()
        # the CODE REQUEST body
        assert "=== CODE REQUEST ===" in delivered
        assert "## Spec to implement" in delivered
        # the tool-neutral rules, verbatim: the exact `sh tools/test.sh` form
        # appears only when the project declares a test command; here it does
        # not, so assert the tool-neutral wording that must hold either way
        assert "test command" in delivered
        assert "full verify command" in delivered
        assert "Do not commit" in delivered
        assert "scratch files" in delivered
        # pump-only tool instructions must NOT be delivered to the CLI
        assert "run_tests tool" not in delivered
        assert "=== TESTS ===" not in delivered
        _no_key_anywhere(client, story["id"])
    finally:
        client.__exit__(None, None, None)


class ApprovingGateway(FakeGateway):
    """FakeGateway, but every review ends in a parsable APPROVED verdict.

    The CODE stage runs the fake CLI; spec / AI-review / code-review phases
    still go through the pump gateway, and those verdicts must parse.

    Pump CODE stages (used by the executor-report tests to land an `internal`
    story for comparison) get a `write_file` tool call, so the worktree ends
    up with a real diff for the code reviewer to approve.
    """

    def __init__(self):
        super().__init__()
        self.coded = False

    async def stream_chat(
        self, *, model, messages, tools=None, max_output_tokens=None, effort=None
    ):
        from app.providers import Delta, ToolCall, Usage

        self.calls.append({"model": model, "messages": messages, "effort": effort})
        last = messages[-1].get("content") if messages else ""
        if isinstance(last, str) and last.startswith("=== SPEC REQUEST ==="):
            yield Delta(text=MINIMAL_SPEC)
        elif isinstance(last, str) and last.startswith("=== CODE REQUEST ==="):
            assert any(t["function"]["name"] == "write_file" for t in tools or [])
            yield ToolCall(
                id="hw1",
                name="write_file",
                arguments=json.dumps(
                    {"path": "feature.py", "content": "VALUE = 'pump'\n"}
                ),
            )
            yield Delta(text="wrote feature.py")
        else:
            yield Delta(text="looks good\n\nVERDICT: APPROVED")
        yield Usage(prompt_tokens=3, completion_tokens=5)


def _headless_client(
    tmp_path, monkeypatch, gateway, mode="ok", *, fake_env=None, app_gateway=None,
    **settings
):
    # the fake CLI's mode (and any test hooks) are baked into the wrapper:
    # the headless child env is a closed contract, so ambient env can never
    # reach the child (an ambient marker would silently skip the assertion)
    bindir = install_fake_claude(tmp_path, mode, env=fake_env or {})
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / f"headless-{mode}.db"))
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(gateway=app_gateway or ApprovingGateway())
    client = TestClient(app)
    client.__enter__()
    # the runner refuses to CODE without an app-level project_root (the
    # project's own root only overrides it); write_tools gates execution
    repo = _init_repo(tmp_path / "app-root")
    body = {
        "story_poll_interval": 0.05,
        "write_tools_enabled": True,
        "project_root": repo,
    }
    body.update(settings)
    client.patch("/api/v1/settings", json=body)
    return client


def _run_headless_story(client, tmp_path, model, title: str) -> str:
    """Queue a headless story and push it through the spec gate.

    Returns the story id once the CODE stage is underway (status leaves
    human_review after the spec approval).
    """
    proj = client.post(
        "/api/v1/projects",
        json={"name": "demo", "root": _init_repo(tmp_path / "repo-dir")},
    ).json()
    story = client.post(
        "/api/v1/stories",
        json={
            "title": title,
            "project_id": proj["id"],
            "execution": "headless",
            "model": model,
        },
    ).json()
    assert story["execution"] == "headless"
    client.post(f"/api/v1/stories/{story['id']}/queue")
    _wait_for_status(client, story["id"], "human_review", 60)
    client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
    return story["id"]


def test_headless_happy_path_traverses_the_pipeline(tmp_path, monkeypatch, gateway):
    client = _headless_client(tmp_path, monkeypatch, gateway, "ok")
    try:
        model = _seed_provider(client)
        proj = client.post(
            "/api/v1/projects",
            json={"name": "demo", "root": _init_repo(tmp_path / "repo-dir")},
        ).json()
        story = client.post(
            "/api/v1/stories",
            json={
                "title": "Add feature",
                "project_id": proj["id"],
                "execution": "headless",
                "model": model,
            },
        ).json()
        assert story["execution"] == "headless"
        client.post(f"/api/v1/stories/{story['id']}/queue")

        # spec gate first: approve the spec, then the CODE stage runs
        _wait_for_status(client, story["id"], "human_review", 60)
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        final = _wait_for_status(client, story["id"], "human_review", 120)
        # the code reviewer must have seen a diff and approved it
        assert final["ai_verdict"] == "approved" or final.get("ai_review"), final

        rounds = [
            e
            for e in _events(client, story["id"], "round")
            if e["data"].get("executor") == "headless"
        ]
        assert rounds, "expected a headless coder round event"
        assert rounds[0]["data"]["num_turns"] == 3
        assert isinstance(rounds[0]["data"]["duration_ms"], int)
        results = [
            e for e in _events(client, story["id"], "note") if e["data"].get("headless_result")
        ]
        assert results, "expected a headless_result note"
        assert results[0]["data"]["usage"]["input_tokens"] == 120
        assert results[0]["data"]["num_turns"] == 3
        assert "feature.py" in results[0]["data"]["result"]
        # LIVE WATCH: the session was relayed as story events while it ran
        stream = _events(client, story["id"], "headless_stream")
        assert stream, "expected relayed headless stream events"
        types = [e["data"].get("type") for e in stream]
        assert types[0] == "assistant"
        assert "tool_use" in types and "tool_result" in types
        assert all(e["data"].get("executor") == "headless" for e in stream)
        # verify ran (the story reached ai_review then human_review)
        statuses = [
            e["data"] for e in _events(client, story["id"], "status")
        ]
        assert any(s.get("to") == "ai_review" for s in statuses)
        # the coder's work is in the worktree, committed
        wt_notes = [
            e for e in _events(client, story["id"], "note") if e["data"].get("worktree")
        ]
        assert wt_notes
        wt = Path(wt_notes[0]["data"]["worktree"])
        assert (wt / "feature.py").exists(), "the fake CLI's work must land in the worktree"
        assert final["base_ref"]
        # KEEP THE TRANSCRIPT: the artifact is recorded and served read-only
        transcript_notes = [
            e
            for e in _events(client, story["id"], "note")
            if e["data"].get("headless_transcript")
        ]
        assert transcript_notes, "expected a headless_transcript note"
        res = client.get(f"/api/v1/stories/{story['id']}/headless-transcript")
        assert res.status_code == 200, res.text
        assert SECRET_KEY not in res.text, "key leaked into the served transcript"
        assert '"type": "user"' in res.text
        _no_key_anywhere(client, story["id"])
    finally:
        client.__exit__(None, None, None)


CONFLICT_MARK = "=== MERGE CONFLICT RESOLUTION REQUEST ==="


class PromptRecordingGateway(ApprovingGateway):
    """ApprovingGateway that remembers every user prompt it was sent."""

    def __init__(self):
        super().__init__()
        self.prompts: list[str] = []

    async def stream_chat(self, *, messages, **kw):
        last = messages[-1].get("content") if messages else ""
        self.prompts.append(last if isinstance(last, str) else "")
        async for ev in super().stream_chat(messages=messages, **kw):
            yield ev


def _headless_conflict_story(client, tmp_path) -> tuple[str, str]:
    """Run a headless story to its first review gate, let main edit the file
    the fake coder wrote, then reject it so the rerun merges main in and hits
    a conflict. Returns (story_id, project_root)."""
    model = _seed_provider(client)
    repo = _init_repo(tmp_path / "repo-dir")
    proj = client.post("/api/v1/projects", json={"name": "demo", "root": repo}).json()
    story = client.post(
        "/api/v1/stories",
        json={
            "title": "Conflicting",
            "project_id": proj["id"],
            "execution": "headless",
            "model": model,
        },
    ).json()
    sid = story["id"]
    client.post(f"/api/v1/stories/{sid}/queue")
    _wait_for_status(client, sid, "human_review", 60)
    client.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
    _wait_for_status(client, sid, "human_review", 120)
    (Path(repo) / "feature.py").write_text("VALUE = 'main'\n")
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", repo,
         "commit", "-qm", "main edits feature.py"],
        check=True, capture_output=True,
    )
    client.post(f"/api/v1/stories/{sid}/reject", json={"reason": "rerun"})
    return sid, repo


def _headless_transcripts(client, sid) -> list[dict]:
    return [
        e for e in _events(client, sid, "note") if e["data"].get("headless_transcript")
    ]


def test_headless_conflict_round_runs_on_the_cli_not_the_gateway(
    tmp_path, monkeypatch, gateway
):
    gw = PromptRecordingGateway()
    client = _headless_client(
        tmp_path, monkeypatch, gateway, "conflict_markers", app_gateway=gw
    )
    try:
        sid, _repo = _headless_conflict_story(client, tmp_path)
        final = _wait_for_status(client, sid, ("human_review", "failed"), 120)
        assert final["status"] == "human_review", final.get("error")
        assert "feature.py" in (final.get("error") or "")
        assert not [p for p in gw.prompts if p.startswith(CONFLICT_MARK)], (
            "the conflict round went through the internal agent loop"
        )
        # first round, reject rerun and conflict round each ran on the CLI
        rounds = [
            e for e in _events(client, sid, "note")
            if e["data"].get("executor") == "headless"
        ]
        assert len(rounds) >= 3
    finally:
        client.__exit__(None, None, None)


def test_headless_conflict_round_resolved_continues(tmp_path, monkeypatch, gateway):
    client = _headless_client(tmp_path, monkeypatch, gateway, "conflict_resolve")
    try:
        sid, _repo = _headless_conflict_story(client, tmp_path)
        deadline = time.monotonic() + 120
        statuses: list[str] = []
        while time.monotonic() < deadline:
            statuses = [e["data"].get("to") for e in _events(client, sid, "status")]
            if "ai_review" in statuses:
                break
            cur = client.get(f"/api/v1/stories/{sid}").json()
            assert cur["status"] != "failed", cur.get("error")
            time.sleep(0.1)
        assert "ai_review" in statuses, statuses
        final = _wait_for_status(client, sid, ("human_review", "failed"), 120)
        assert final["status"] == "human_review", final.get("error")
        assert "conflicts" not in (final.get("error") or "")
        # the conflict round itself kept a transcript: one lands after the
        # merge-with-main conflicts note
        evs = _events(client, sid)
        conflict_at = next(
            i for i, e in enumerate(evs)
            if e["kind"] == "note"
            and e["data"].get("phase") == "merge-with-main"
            and "conflicts" in e["data"]
        )
        # The kept transcripts of one second share a file name, so the file
        # content cannot tell rounds apart; the event order can: the conflict
        # round's own "round" event is immediately followed by its transcript.
        round_at = next(
            i for i, e in enumerate(evs) if i > conflict_at and e["kind"] == "round"
        )
        nxt = evs[round_at + 1]
        assert nxt["kind"] == "note" and nxt["data"].get("headless_transcript")
    finally:
        client.__exit__(None, None, None)


def test_headless_conflict_round_leaving_markers_parks_with_paths(
    tmp_path, monkeypatch, gateway
):
    client = _headless_client(tmp_path, monkeypatch, gateway, "conflict_markers")
    try:
        sid, _repo = _headless_conflict_story(client, tmp_path)
        final = _wait_for_status(client, sid, ("human_review", "failed"), 120)
        assert final["status"] == "human_review", final.get("error")
        assert "merge with main has conflicts: feature.py" in (final.get("error") or "")
    finally:
        client.__exit__(None, None, None)


def test_headless_conflict_round_crash_parks_never_fails(tmp_path, monkeypatch, gateway):
    client = _headless_client(tmp_path, monkeypatch, gateway, "conflict_crash")
    try:
        sid, _repo = _headless_conflict_story(client, tmp_path)
        final = _wait_for_status(client, sid, ("human_review", "failed"), 120)
        assert final["status"] == "human_review", final.get("error")
        assert "conflict round failed" in (final.get("error") or "")
        assert "feature.py" in (final.get("error") or "")
        errs = [
            e for e in _events(client, sid, "note")
            if e["data"].get("conflict_round_error")
        ]
        assert errs and errs[0]["data"]["conflicts"] == ["feature.py"]
        assert not [
            e for e in _events(client, sid, "status") if e["data"].get("to") == "failed"
        ]
    finally:
        client.__exit__(None, None, None)


def test_headless_fix_round_crash_parks_never_fails(tmp_path, monkeypatch, gateway):
    client = _headless_client(tmp_path, monkeypatch, gateway, "fix_crash")
    try:
        model = _seed_provider(client)
        client.patch("/api/v1/settings", json={"verify_command": "test -f fixed.txt"})
        sid = _run_headless_story(client, tmp_path, model, "Fix crash")
        final = _wait_for_status(client, sid, ("human_review", "failed"), 120)
        assert final["status"] == "human_review", final.get("error")
        assert "fix round" in (final.get("error") or "")
        assert [
            e for e in _events(client, sid, "note") if e["data"].get("fix_round_error")
        ]
        assert not [
            e for e in _events(client, sid, "status") if e["data"].get("to") == "failed"
        ]
    finally:
        client.__exit__(None, None, None)


def test_headless_fix_round_runs_on_the_cli_not_the_gateway(
    tmp_path, monkeypatch, gateway
):
    gw = PromptRecordingGateway()
    client = _headless_client(tmp_path, monkeypatch, gateway, "fix_ok", app_gateway=gw)
    try:
        model = _seed_provider(client)
        client.patch("/api/v1/settings", json={"verify_command": "test -f fixed.txt"})
        sid = _run_headless_story(client, tmp_path, model, "Fix ok")
        final = _wait_for_status(client, sid, ("human_review", "failed"), 120)
        assert final["status"] == "human_review", final.get("error")
        evs = _events(client, sid)
        assert not [
            e for e in evs if e["kind"] == "status" and e["data"].get("to") == "failed"
        ]
        verifies = [e for e in evs if e["kind"] == "verify"]
        assert [(v["data"]["attempt"], v["data"]["passed"]) for v in verifies] == [
            (1, False),
            (2, True),
        ]
        first_verify = evs.index(verifies[0])
        assert any(
            e["kind"] == "note" and e["data"].get("headless_transcript")
            for e in evs[first_verify + 1:]
        )
        assert not [p for p in gw.prompts if p.startswith("=== FIX REQUEST ===")]
    finally:
        client.__exit__(None, None, None)


def test_headless_denials_become_a_note(tmp_path, monkeypatch, gateway):
    """BLOCKED TOOLS VISIBLE: permission denials surface as a note."""
    client = _headless_client(tmp_path, monkeypatch, gateway, "denials")
    try:
        model = _seed_provider(client)
        sid = _run_headless_story(client, tmp_path, model, "Denied")
        deadline = time.monotonic() + 60
        noted = None
        while time.monotonic() < deadline:
            noted = next(
                (n for n in _notes(client, sid) if "permission denied" in n.get("body", "")),
                None,
            )
            if not noted:
                time.sleep(0.2)
                continue
            break
        assert noted, "permission denials must become a story note"
        assert "Bash" in noted["body"] and "rm -rf" in noted["body"]
        # the event carries the structured denials
        denial_events = [
            e["data"]
            for e in _events(client, sid, "note")
            if e["data"].get("headless_denied_tools")
        ]
        assert denial_events
        assert denial_events[0]["denials"][0]["tool"] == "Bash"
        assert "rm -rf" in denial_events[0]["denials"][0]["input"]
        assert SECRET_KEY not in json.dumps(_notes(client, sid))
        _no_key_anywhere(client, sid)
    finally:
        client.__exit__(None, None, None)


def test_headless_missing_result_fails_the_story(tmp_path, monkeypatch, gateway):
    client = _headless_client(tmp_path, monkeypatch, gateway, "noresult")
    try:
        model = _seed_provider(client)
        sid = _run_headless_story(client, tmp_path, model, "NoResult")
        final = _wait_for_status(client, sid, "failed", 120)
        assert "without a result event" in (final.get("error") or "")
        _no_key_anywhere(client, sid)
    finally:
        client.__exit__(None, None, None)


def test_headless_transcript_endpoint_404_for_pump_stories(client):
    """A story that never ran headless has no transcript to serve."""
    res = client.get("/api/v1/stories/whatever/headless-transcript")
    assert res.status_code == 404


def test_headless_crash_fails_the_story(tmp_path, monkeypatch, gateway):
    client = _headless_client(tmp_path, monkeypatch, gateway, "crash")
    try:
        model = _seed_provider(client)
        sid = _run_headless_story(client, tmp_path, model, "Crashy")
        final = _wait_for_status(client, sid, "failed", 120)
        assert "headless exit 3" in (final.get("error") or "")
        assert "claude crashed while planning edits" in (final.get("error") or "")
        _no_key_anywhere(client, sid)
    finally:
        client.__exit__(None, None, None)


def test_headless_rate_limit_requeues_without_burning_a_fix_round(
    tmp_path, monkeypatch, gateway
):
    client = _headless_client(tmp_path, monkeypatch, gateway, "ratelimit")
    try:
        model = _seed_provider(client)
        sid = _run_headless_story(client, tmp_path, model, "Limited")
        # the story requeues (backoff) instead of failing or parking
        deadline = time.monotonic() + 30
        requeued = False
        while time.monotonic() < deadline:
            s = client.get(f"/api/v1/stories/{sid}").json()
            if s["status"] == "queued":
                requeued = True
                break
            assert s["status"] != "failed", s.get("error")
            time.sleep(0.1)
        assert requeued, "429 stderr must requeue the story"
        # no fix-round feedback note was written
        feedback_notes = [
            e
            for e in _events(client, sid, "note")
            if "Previous attempt feedback" in e["raw"]
        ]
        assert feedback_notes == []
        _no_key_anywhere(client, sid)
    finally:
        client.__exit__(None, None, None)


def _fast_requeue(monkeypatch):
    """Shrink _requeue_for_rate_limit's real backoff to ~0.5s (the
    _fast_backoff trick from test_stories.py) so a cleared provider hold
    shows a new run inside the test's lifetime."""
    from app import runner as runner_mod

    original = runner_mod.StoryRunner._requeue_for_rate_limit

    def fast_requeue(self, story_id, error, reason="rate limit", **kw):
        original(self, story_id, error, reason=reason, **kw)
        if story_id in self._backoff:  # shrink the real backoff to ~0.5s
            not_before, attempt = self._backoff[story_id]
            self._backoff[story_id] = (time.monotonic() + 0.5, attempt)

    monkeypatch.setattr(runner_mod.StoryRunner, "_requeue_for_rate_limit", fast_requeue)


def test_headless_stream_limit_requeues_holds_provider_and_spares_dependent(
    tmp_path, monkeypatch, gateway
):
    """The 26 Oct incident shape end to end: the story requeues (never fails,
    never cascades), the provider is held until the parsed reset time (read in
    the provider's zone), and the note names the limit."""
    provider_zone = timezone(timedelta(hours=8))
    reset_local = (
        datetime.now(provider_zone).replace(microsecond=0, tzinfo=None)
        + timedelta(hours=1)
    )
    client = _headless_client(
        tmp_path,
        monkeypatch,
        gateway,
        "streamlimit",
        fake_env={"FAKE_CLAUDE_RESET_AT": reset_local.strftime("%Y-%m-%d %H:%M:%S")},
    )
    try:
        model = _seed_provider(client)
        proj = client.post(
            "/api/v1/projects",
            json={"name": "demo", "root": _init_repo(tmp_path / "repo-dir")},
        ).json()
        main = client.post(
            "/api/v1/stories",
            json={
                "title": "StreamLimited",
                "project_id": proj["id"],
                "execution": "headless",
                "model": model,
            },
        ).json()
        dependent = client.post(
            "/api/v1/stories",
            json={
                "title": "Depends",
                "project_id": proj["id"],
                "execution": "headless",
                "model": model,
                "depends_on": [main["id"]],
            },
        ).json()
        client.post(f"/api/v1/stories/{main['id']}/queue")
        client.post(f"/api/v1/stories/{dependent['id']}/queue")
        _wait_for_status(client, main["id"], "human_review", 60)
        client.post(f"/api/v1/stories/{main['id']}/approve", json={"by": "t"})

        # the stream-limit answer requeues the story — never failed
        deadline = time.monotonic() + 30
        requeued = False
        while time.monotonic() < deadline:
            s = client.get(f"/api/v1/stories/{main['id']}").json()
            if s["status"] == "queued":
                requeued = True
                break
            assert s["status"] != "failed", s.get("error")
            time.sleep(0.1)
        assert requeued, "a stream usage-limit answer must requeue the story"

        # the dependent is NOT cascaded into failure
        dep = client.get(f"/api/v1/stories/{dependent['id']}").json()
        assert dep["status"] == "queued", dep.get("error")

        # the provider is held until the parsed reset time, converted from
        # the provider's zone (UTC+8) to UTC
        expected = reset_local.replace(tzinfo=provider_zone).astimezone(UTC)
        holds = client.get("/api/v1/providers/limits").json()
        entry = next(h for h in holds if h["provider_id"] == "zai")
        assert datetime.fromisoformat(entry["until"]) == expected

        # the note names the limit and its reset time
        limit_notes = [
            e["data"]
            for e in _events(client, main["id"], "note")
            if e["data"].get("provider_limit")
        ]
        assert limit_notes
        assert limit_notes[0]["limit"] == "Usage limit reached for 5 hour"
        assert limit_notes[0]["reset_at"] == expected.isoformat()
        _no_key_anywhere(client, main["id"])
    finally:
        client.__exit__(None, None, None)


def test_clearing_provider_hold_lets_pickup_resume(tmp_path, monkeypatch, gateway):
    """While the provider is held, a backoff-expired story is NOT picked up;
    clearing the hold releases the next coder run."""
    _fast_requeue(monkeypatch)
    client = _headless_client(tmp_path, monkeypatch, gateway, "streamlimit")
    try:
        model = _seed_provider(client)
        sid = _run_headless_story(client, tmp_path, model, "HeldThenCleared")

        def transient_rounds() -> list[dict]:
            return [
                e["data"]
                for e in _events(client, sid, "round")
                if e["data"].get("outcome") == "transient"
            ]

        # first stream-limit round + the provider hold on it
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not transient_rounds():
            time.sleep(0.1)
        assert transient_rounds(), "the stream-limit round never happened"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not client.get(
            "/api/v1/providers/limits"
        ).json():
            time.sleep(0.1)
        assert client.get("/api/v1/providers/limits").json(), "provider hold missing"

        # with fast backoff the story WOULD rerun within ~1s — the hold must
        # gate it: no second transient round while the provider is held
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            assert len(transient_rounds()) == 1, "the hold did not gate pickup"
            time.sleep(0.1)

        # clear: 204, the gate releases, a NEW coder round runs
        assert client.post("/api/v1/providers/zai/limits/clear").status_code == 204
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and len(transient_rounds()) < 2:
            time.sleep(0.1)
        assert len(transient_rounds()) >= 2, "clearing the hold never released a new run"
        _no_key_anywhere(client, sid)
    finally:
        client.__exit__(None, None, None)


def test_headless_timeout_fails_the_story(tmp_path, monkeypatch, gateway):
    marker = tmp_path / "child.pid"
    client = _headless_client(
        tmp_path,
        monkeypatch,
        gateway,
        "sleep",
        fake_env={"FAKE_CLAUDE_SLEEP_MARKER": str(marker)},
        headless_timeout_seconds=30,  # the API floor (ge=30)
    )
    try:
        model = _seed_provider(client)
        sid = _run_headless_story(client, tmp_path, model, "Slow")
        final = _wait_for_status(client, sid, "failed", 180)
        assert "headless timeout" in (final.get("error") or "")
        # the child process group was reaped
        if marker.exists():
            child_pid = int(marker.read_text().strip())
            # not a pipeline wait: grace window for the OS to reap the killed child
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                    time.sleep(0.1)
                except ProcessLookupError:
                    break
            else:
                pytest.fail("the timed-out child survived killpg")
        _no_key_anywhere(client, sid)
        _no_key_anywhere(client, sid)
    finally:
        client.__exit__(None, None, None)


# ---- executor report ------------------------------------------------------


def test_executor_report_aggregates_per_executor(tmp_path, monkeypatch, gateway):
    client = _headless_client(tmp_path, monkeypatch, gateway, "ok")
    try:
        model = _seed_provider(client)
        repo = _init_repo(tmp_path / "repo-dir")
        proj = client.post(
            "/api/v1/projects", json={"name": "rep", "root": repo}
        ).json()

        # one headless story that lands (2 CODE runs: verify needs a fix?)
        hs = client.post(
            "/api/v1/stories",
            json={
                "title": "headless one",
                "project_id": proj["id"],
                "execution": "headless",
                "model": model,
            },
        ).json()
        client.post(f"/api/v1/stories/{hs['id']}/queue")
        # spec gate first, then the CODE stage, then the code gate
        _wait_for_status(client, hs["id"], "human_review", 120)
        client.post(f"/api/v1/stories/{hs['id']}/approve", json={"by": "t"})
        _wait_for_status(client, hs["id"], "human_review", 120)
        client.post(f"/api/v1/stories/{hs['id']}/approve", json={"by": "t"})
        _wait_for_status(client, hs["id"], "done", 60)

        # one pump story: drive it through the gateway instead
        ps = client.post(
            "/api/v1/stories",
            json={
                "title": "pump one",
                "project_id": proj["id"],
                "execution": "internal",
                "model": model,
            },
        ).json()
        client.post(f"/api/v1/stories/{ps['id']}/queue")
        _wait_for_status(client, ps["id"], "human_review", 120)
        client.post(f"/api/v1/stories/{ps['id']}/approve", json={"by": "t"})
        _wait_for_status(client, ps["id"], "human_review", 120)
        client.post(f"/api/v1/stories/{ps['id']}/approve", json={"by": "t"})
        _wait_for_status(client, ps["id"], "done", 60)

        res = client.get(f"/api/v1/projects/{proj['id']}/executor-report")
        assert res.status_code == 200, res.text
        report = res.json()
        by_name = {row["executor"]: row for row in report["executors"]}
        assert set(by_name) == {"internal", "headless"}
        headless = by_name["headless"]
        pump = by_name["internal"]
        assert headless["runs"] >= 1
        assert headless["wall_ms_total"] > 0
        assert headless["landed"] == 1
        assert headless["rounds_to_land_avg"] >= 1.0
        assert pump["runs"] >= 1
        assert pump["landed"] == 1
        assert report["ab_headless_share"] == 0.0
        # wall_ms_avg derived from the same runs
        assert headless["wall_ms_avg"] == round(
            headless["wall_ms_total"] / headless["runs"], 1
        )
    finally:
        client.__exit__(None, None, None)


def test_executor_report_counts_pickup_assigned_story(tmp_path, monkeypatch, gateway):
    """A story the scheduler's PICKUP draw routed headless (queue-time share
    was still 0) is attributed to the headless report row, not the pump's."""
    client = _headless_client(tmp_path, monkeypatch, gateway, "ok")
    try:
        model = _seed_provider(client)
        # park the scheduler so the story queues while the share is still 0
        client.patch("/api/v1/settings", json={"story_poll_interval": 9999})
        repo = _init_repo(tmp_path / "repo-dir")
        proj = client.post(
            "/api/v1/projects", json={"name": "pick", "root": repo}
        ).json()
        story = client.post(
            "/api/v1/stories",
            json={"title": "pickup headless", "project_id": proj["id"], "model": model},
        ).json()
        assert story["execution_override"] is None
        client.post(f"/api/v1/stories/{story['id']}/queue")
        # arm the share, then unpark: the pickup draw must materialize it
        res = client.patch(
            f"/api/v1/projects/{proj['id']}", json={"ab_headless_share": 1.0}
        )
        assert res.status_code == 200, res.text
        client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})

        _wait_for_status(client, story["id"], "human_review", 120)
        picked = client.get(f"/api/v1/stories/{story['id']}").json()
        assert picked["execution_override"] == "headless"
        # spec gate, then the headless CODE stage, then the code gate
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        _wait_for_status(client, story["id"], "human_review", 120)
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        _wait_for_status(client, story["id"], "done", 60)

        res = client.get(f"/api/v1/projects/{proj['id']}/executor-report")
        assert res.status_code == 200, res.text
        report = res.json()
        assert report["ab_headless_share"] == 1.0
        by_name = {row["executor"]: row for row in report["executors"]}
        assert by_name["headless"]["runs"] >= 1
        assert by_name["headless"]["landed"] == 1
        # nothing leaked into the pump row: pickup attribution is the point
        assert by_name["internal"]["runs"] == 0
        assert by_name["internal"]["landed"] == 0
    finally:
        client.__exit__(None, None, None)


def test_executor_report_counts_rejections(tmp_path, monkeypatch, gateway):
    """AI-review and human rejections land on the right executor row."""
    client = _headless_client(tmp_path, monkeypatch, gateway, "ok")
    try:
        model = _seed_provider(client)
        repo = _init_repo(tmp_path / "repo-dir")
        proj = client.post(
            "/api/v1/projects", json={"name": "rej", "root": repo}
        ).json()
        story = client.post(
            "/api/v1/stories",
            json={
                "title": "rejected",
                "project_id": proj["id"],
                "execution": "headless",
                "model": model,
            },
        ).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        _wait_for_status(client, story["id"], "human_review", 120)
        client.post(
            f"/api/v1/stories/{story['id']}/reject",
            json={"reason": "not good enough", "by": "human"},
        )
        # back to queued; give it a moment to re-run, then reject again
        _wait_for_status(client, story["id"], "human_review", 120)
        client.post(
            f"/api/v1/stories/{story['id']}/reject",
            json={"reason": "still not good", "by": "human"},
        )
        res = client.get(f"/api/v1/projects/{proj['id']}/executor-report")
        headless = {
            r["executor"]: r for r in res.json()["executors"]
        }["headless"]
        assert headless["human_rejections"] >= 2
    finally:
        client.__exit__(None, None, None)


def test_executor_report_ignores_external_stories(tmp_path, monkeypatch, gateway):
    client = _headless_client(tmp_path, monkeypatch, gateway, "ok")
    try:
        repo = _init_repo(tmp_path / "repo-dir")
        proj = client.post(
            "/api/v1/projects", json={"name": "ext", "root": repo}
        ).json()
        ext = client.post(
            "/api/v1/stories",
            json={
                "title": "outside",
                "project_id": proj["id"],
                "execution": "external",
            },
        ).json()
        client.post(f"/api/v1/stories/{ext['id']}/queue")
        res = client.get(f"/api/v1/projects/{proj['id']}/executor-report")
        assert res.status_code == 200
        for row in res.json()["executors"]:
            assert row["runs"] == 0
            assert row["landed"] == 0
    finally:
        client.__exit__(None, None, None)


def test_executor_report_unknown_project_404(client):
    assert (
        client.get("/api/v1/projects/does-not-exist/executor-report").status_code == 404
    )


# ---- full suite outside the session (FUBALA_RUN_TESTS) -------------------------


def test_with_rules_asks_for_the_marker_only_with_a_verify_command():
    with_verify = hx.with_rules(
        "=== CODE REQUEST ===", verify_command="make test", test_timeout_seconds=90
    )
    assert "Never run the full suite in the session." in with_verify
    assert "FUBALA_RUN_TESTS" in with_verify
    assert "`make test`" in with_verify
    assert "cut at 90 s" in with_verify
    assert "Run the full verify command before you finish" not in with_verify
    without = hx.with_rules("=== CODE REQUEST ===")
    assert "FUBALA_RUN_TESTS" not in without
    assert "Run the full verify command before you finish." in without


def test_test_timeout_reaches_the_child_env_twice_equal():
    env = hx._child_env(make_config(test_timeout_seconds=2.0))
    assert env["BASH_DEFAULT_TIMEOUT_MS"] == env["BASH_MAX_TIMEOUT_MS"] == "2000"
    assert hx.pod_child_env(make_config(), "/work/.claude/1")["BASH_MAX_TIMEOUT_MS"] == "120000"


def test_run_tests_outcome_keeps_the_config_dir_and_resume_reuses_it(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    capture = tmp_path / "capture.txt"
    bindir = install_fake_claude(
        tmp_path, "runtests", env={"FAKE_CLAUDE_CAPTURE": str(capture)}
    )
    with on_path(bindir):
        first = hx.run_code_stage(story_id="s9", worktree=wt, prompt="go", config=make_config())
        assert first.outcome == "run_tests"
        assert first.session_id == "sess-fake-1"
        assert first.config_dir and Path(first.config_dir).is_dir()
        second = hx.run_code_stage(
            story_id="s9",
            worktree=wt,
            prompt="result",
            config=make_config(),
            resume_session_id=first.session_id,
            config_dir=first.config_dir,
        )
    assert second.outcome == "ok"
    assert second.session_id == "sess-fake-1"
    assert "--resume\nsess-fake-1" in capture.read_text()
    assert not Path(first.config_dir).exists()


def test_marker_without_a_session_id_is_a_fix():
    stream = hx.StreamReader((), lambda _p: None)
    stream.feed(json.dumps({"type": "result", "result": "x\nFUBALA_RUN_TESTS", "num_turns": 1}))
    out = hx.map_outcome(
        exit_code=0, stderr="", stream=stream, config=make_config(), duration_ms=1,
        transcript_path=None,
    )
    assert out.outcome == "fix"


def test_marker_only_counts_as_the_last_line():
    stream = hx.StreamReader((), lambda _p: None)
    stream.feed(json.dumps({
        "type": "result", "session_id": "s", "num_turns": 1,
        "result": "FUBALA_RUN_TESTS\nbut I changed my mind",
    }))
    out = hx.map_outcome(
        exit_code=0, stderr="", stream=stream, config=make_config(), duration_ms=1,
        transcript_path=None,
    )
    assert out.outcome == "ok"


def test_targeted_run_is_cut_and_reported(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    events: list[dict] = []
    started = time.monotonic()
    with on_path(install_fake_claude(tmp_path, "slowtest")):
        out = hx.run_code_stage(
            story_id="s10",
            worktree=wt,
            prompt="go",
            config=make_config(test_timeout_seconds=2.0),
            on_event=events.append,
        )
    assert time.monotonic() - started < 25
    assert out.outcome == "ok"
    assert out.targeted_cuts == 1
    assert events.count({"type": "test_cut"}) == 1
    previews = [e.get("preview", "") for e in events if e.get("type") == "tool_result"]
    assert "Command timed out after 2000ms" in previews
