"""Worker pods: the spec-phase Claude Code sessions run in a worker pod.

With run_executor=pod a claude-max spec (and its one corrective nudge) is a
`claude -p` session exec'd in the story's worker pod, placed like a coder pod.
The API process spawns no `claude` child. The kube client is a fake that
answers the exec frame with the CLI's JSON result.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from conftest import MINIMAL_SPEC
from test_claude_code_phases import MODEL, TOKEN, _story, _wait_spec
from test_headless import _events, _headless_client, _init_repo
from test_pod_executor import FakeKube

from app import pods
from app.providers import claude_code as cc


@pytest.fixture(autouse=True)
def pod_env(monkeypatch):
    monkeypatch.setenv("POD_NAME", "fubala-0")
    monkeypatch.setenv("POD_UID", "uid-123")
    monkeypatch.setenv("NODE_NAME", "stor01")
    monkeypatch.setenv("FUBALA_WORKER_IMAGE_REPO", "registry.example.com/fubala/fubala-worker")
    pods._WARNED.clear()
    pods._FALLBACK_HELD.clear()
    yield
    pods.set_kube_factory(None)
    pods._LIVE.clear()
    pods._FALLBACK_HELD.clear()


def parse_frame(frame: bytes) -> tuple[dict[str, str], list[str]]:
    """Inverse of pods.headless_frame: (env, argv)."""
    head, _, rest = frame.partition(b"\n")
    _timeout, n_env, n_args = (int(x) for x in head.split())
    env: dict[str, str] = {}
    for _ in range(n_env):
        line, _, rest = rest.partition(b"\n")
        key, _, size = line.partition(b" ")
        env[key.decode()], rest = rest[: int(size)].decode(), rest[int(size) :]
    argv: list[str] = []
    for _ in range(n_args):
        line, _, rest = rest.partition(b"\n")
        argv.append(rest[: int(line)].decode())
        rest = rest[int(line) :]
    return env, argv


def cli_json(text: str = MINIMAL_SPEC) -> str:
    return json.dumps(
        {"type": "result", "is_error": False, "result": text,
         "usage": {"input_tokens": 4, "output_tokens": 9}}
    )  # fmt: skip


class SpecKube(FakeKube):
    """Answers the CLI exec with `results` in turn (the last one repeats)."""

    def __init__(self, results: list[str] | None = None, **kw):
        super().__init__(**kw)
        self.results = results or [cli_json()]
        self.frames: list[bytes] = []

    def exec(self, name, argv, stdin_bytes, on_output, timeout):
        if argv[:2] == ["sh", "-c"] and argv[2] == pods.HEADLESS_SCRIPT:
            self.execs.append((argv, stdin_bytes))
            self.frames.append(stdin_bytes or b"")
            out = self.results.pop(0) if len(self.results) > 1 else self.results[0]
            return 0, out, ""
        return super().exec(name, argv, stdin_bytes, on_output, timeout)


@pytest.fixture
def no_local_cli(monkeypatch):
    """Any local claude child process fails the test."""
    calls: list[object] = []

    async def spawn(*a, **kw):
        calls.append(a)
        raise AssertionError("claude_code._spawn called")

    async def create_subprocess_exec(*a, **kw):
        calls.append(a)
        raise AssertionError("create_subprocess_exec called")

    monkeypatch.setattr(cc, "_spawn", spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
    return calls


def pod_client(tmp_path, monkeypatch, gateway, kube, mode="pod", **extra):
    pods.set_kube_factory(lambda: kube)
    return _headless_client(
        tmp_path, monkeypatch, gateway, run_executor=mode, worker_image="20261102-1", **extra
    )


def test_spec_session_runs_in_one_worker_pod(tmp_path, monkeypatch, gateway, no_local_cli):
    kube = SpecKube()
    client = pod_client(tmp_path, monkeypatch, gateway, kube)
    try:
        story, _proj = _story(client, tmp_path)
        detail = _wait_spec(client, story["id"])
        assert "## Goal" in detail["spec"]
        assert len(kube.created) == 1
        assert len(kube.frames) == 1
        env, argv = parse_frame(kube.frames[0])
        assert argv[0] == "claude"
        assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
        assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
        assert "=== SPEC REQUEST ===" in argv[argv.index("-p") + 1]
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
        assert env["CLAUDE_CONFIG_DIR"].startswith(pods.CLAUDE_DIR)
        # the token never rides in a kube exec argv, a pod spec or an event
        assert TOKEN not in json.dumps([a for a, _ in kube.execs])
        assert TOKEN not in json.dumps(kube.created)
        for e in _events(client, story["id"]):
            assert TOKEN not in e["raw"]
        assert no_local_cli == []
        # the spec lands in AI review (or beyond), never failed
        deadline = time.monotonic() + 30
        status = None
        while time.monotonic() < deadline:
            status = client.get(f"/api/v1/stories/{story['id']}").json()["status"]
            if status != "in_progress":
                break
            time.sleep(0.1)
        assert status != "failed"
    finally:
        client.__exit__(None, None, None)


def test_nudge_runs_in_the_same_pod(tmp_path, monkeypatch, gateway, no_local_cli):
    kube = SpecKube([cli_json("just narration"), cli_json()])
    client = pod_client(tmp_path, monkeypatch, gateway, kube)
    try:
        story, _proj = _story(client, tmp_path)
        _wait_spec(client, story["id"])
        assert len(kube.created) == 1
        assert len(kube.frames) == 2
        _env, argv = parse_frame(kube.frames[1])
        assert argv[argv.index("--tools") + 1] == ""
        assert "--max-turns" not in argv
        assert "[system] Your previous output was not a usable spec" in argv[argv.index("-p") + 1]
        assert no_local_cli == []
    finally:
        client.__exit__(None, None, None)


def test_fallback_cap_full_requeues_the_story(tmp_path, monkeypatch, gateway, no_local_cli):
    for other in ("other-1", "other-2"):
        assert pods.try_claim_fallback(other, 2, None)
    kube = SpecKube(node="stor01")  # the pod lands on the backend node
    client = pod_client(tmp_path, monkeypatch, gateway, kube)
    try:
        story, _proj = _story(client, tmp_path)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not [
            e for e in _events(client, story["id"], "status")
            if e["data"].get("fallback_cap_wait")
        ]:
            time.sleep(0.1)
        waits = [
            e for e in _events(client, story["id"], "status")
            if e["data"].get("fallback_cap_wait")
        ]
        assert waits and waits[0]["data"]["to"] == "queued"
        assert kube.frames == []
        assert client.get(f"/api/v1/stories/{story['id']}").json()["status"] != "failed"
        assert no_local_cli == []
    finally:
        client.__exit__(None, None, None)


def test_pod_unavailable_runs_the_session_locally(tmp_path, monkeypatch, gateway):
    kube = SpecKube(create_error=RuntimeError("quota exceeded"))
    local: list[list[str]] = []

    async def fake_spawn(argv, *, env, cwd, stdin, timeout):
        local.append(argv)
        return 0, cli_json().encode(), b""

    monkeypatch.setattr(cc, "_spawn", fake_spawn)
    client = pod_client(tmp_path, monkeypatch, gateway, kube)
    try:
        story, _proj = _story(client, tmp_path)
        detail = _wait_spec(client, story["id"])
        assert "## Goal" in detail["spec"]
        assert kube.frames == []
        assert len(local) == 1
    finally:
        client.__exit__(None, None, None)


def test_in_process_mode_builds_no_pod(tmp_path, monkeypatch, gateway):
    kube = SpecKube()
    local: list[list[str]] = []

    async def fake_spawn(argv, *, env, cwd, stdin, timeout):
        local.append(argv)
        return 0, cli_json().encode(), b""

    monkeypatch.setattr(cc, "_spawn", fake_spawn)
    client = pod_client(tmp_path, monkeypatch, gateway, kube, mode="in_process")
    try:
        story, _proj = _story(client, tmp_path)
        _wait_spec(client, story["id"])
        assert kube.created == []
        assert len(local) == 1
    finally:
        client.__exit__(None, None, None)


def test_graph_mcp_config_travels_in_the_pod_argv(tmp_path, monkeypatch, gateway, no_local_cli):
    kube = SpecKube()
    client = pod_client(tmp_path, monkeypatch, gateway, kube)
    try:
        client.post("/api/v1/providers/claude-max/key", json={"key": TOKEN})
        proj = client.post(
            "/api/v1/projects",
            json={
                "name": "demo",
                "root": _init_repo(tmp_path / "proj"),
                "code_graph_enabled": True,
            },
        ).json()
        story = client.post(
            "/api/v1/stories", json={"title": "g", "project_id": proj["id"], "model": MODEL}
        ).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        _wait_spec(client, story["id"])
        _env, argv = parse_frame(kube.frames[0])
        # wrapped: the graph is built in the pod, then the CLI runs
        assert argv[:2] == ["sh", "-c"]
        assert "claude" in argv
        assert "--mcp-config" in argv and "--strict-mcp-config" in argv
        assert "--allowedTools" in argv
    finally:
        client.__exit__(None, None, None)


def test_setting_off_puts_no_graph_in_the_pod_argv(tmp_path, monkeypatch, gateway, no_local_cli):
    kube = SpecKube()
    client = pod_client(tmp_path, monkeypatch, gateway, kube)
    try:
        client.post("/api/v1/providers/claude-max/key", json={"key": TOKEN})
        proj = client.post(
            "/api/v1/projects",
            json={
                "name": "demo",
                "root": _init_repo(tmp_path / "proj"),
                "code_graph_enabled": False,
            },
        ).json()
        story = client.post(
            "/api/v1/stories", json={"title": "g", "project_id": proj["id"], "model": MODEL}
        ).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        _wait_spec(client, story["id"])
        _env, argv = parse_frame(kube.frames[0])
        assert "claude" in argv
        assert "--mcp-config" not in argv and "--strict-mcp-config" not in argv
        # no graph-build wrapper around the CLI
        assert not (argv[:2] == ["sh", "-c"] and "CRG_DATA_DIR" in argv[2])
        assert not any("code-review-graph" in a for a in argv)
    finally:
        client.__exit__(None, None, None)


def test_large_prompt_goes_through_a_file(tmp_path):
    kube = SpecKube()
    ex = pods.PodExecutor(
        "s1", {"worker_image": "x", "headless_timeout_seconds": 2400.0, "verify_timeout": 600.0},
        kube_factory=lambda: kube, mode="pod",
    )  # fmt: skip
    repo = Path(_init_repo(tmp_path))
    big = "x" * (pods.SPILL_BYTES + 1)
    rc, out, _err = ex.run_cli_session(
        ["claude", "-p", "--tools", ""], {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}, big, 60.0, repo
    )
    assert rc == 0 and "result" in out
    _env, argv = parse_frame(kube.frames[0])
    assert argv[:3] == ["sh", "-c", 'f=$1; shift; exec "$@" <"$f"']
    assert big not in argv
    spill = [s for a, s in kube.execs if a[-2:-1] == ["_"] and s == big.encode()]
    assert spill
