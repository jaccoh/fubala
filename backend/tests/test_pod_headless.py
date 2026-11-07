"""Worker pods 3: the headless `claude -p` round runs in the worker pod.

`LocalPodKube` is a fake KubeApi that really executes every exec argv on this
machine with `/work` remapped to a temp dir: the stdin frame parsing, the
in-pod commit and the bundle pull-back all run for real against git, with the
fake `claude` from headless_fake_claude.py as the CLI.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest
from test_headless import (
    SECRET_KEY,
    _events,
    _headless_client,
    _init_repo,
    _no_key_anywhere,
    _run_headless_story,
    _seed_provider,
    _wait_for_status,
    install_fake_claude,
    make_config,
    on_path,
)
from test_pod_executor import SETTINGS, FakeKube
from test_pod_spec import parse_frame

from app import code_graph, pods, procs, workspaces
from app import headless as hx

STORY = "abc123"


def kind_of(argv: list[str]) -> str:
    if argv[0] == "git":
        return "git"
    if argv[:2] == ["sh", "-c"]:
        return {
            pods.HEADLESS_SCRIPT: "headless",
            pods.SYNC_SCRIPT: "sync",
            pods.PULL_SCRIPT: "pull",
            pods.TRANSCRIPT_SCRIPT: "transcript",
            pods.KILL_SCRIPT: "kill",
            pods.REAP_SCRIPT: "reap",
        }.get(argv[2], "run")
    return argv[0]


class LocalPodKube(FakeKube):
    """Runs the exec argv locally; `/work` is `root`. stdout reaches
    `on_output` in `chunk`-sized pieces (lines split across calls)."""

    def __init__(self, root: Path, bindir: Path, *, chunk: int = 7, **kw):
        super().__init__(**kw)
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(root / "cache" / "home"),
        }
        self.chunk = chunk
        self.order: list[str] = []
        self.hooks: dict[str, object] = {}

    def delete_pod(self, name):
        self.order.append("delete")
        super().delete_pod(name)

    def exec(self, name, argv, stdin_bytes, on_output, timeout):
        kind = kind_of(argv)
        self.order.append(kind)
        self.execs.append((list(argv), stdin_bytes))
        hook = self.hooks.get(kind)
        if hook is not None:
            return hook(argv, stdin_bytes, on_output)
        if kind in ("kill", "reap"):  # never signal host processes
            return 0, "", ""
        real = [a.replace("/work", str(self.root)) for a in argv]
        try:
            res = subprocess.run(
                real, input=stdin_bytes or b"", capture_output=True,
                timeout=timeout, env=self.env,
            )  # fmt: skip
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(f"exec exceeded {timeout}") from e
        out = res.stdout.decode("utf-8", "replace")
        if on_output is not None:
            for i in range(0, len(out), self.chunk):
                on_output(out[i : i + self.chunk])
        return res.returncode, out, res.stderr.decode("utf-8", "replace")


@pytest.fixture(autouse=True)
def pod_env(monkeypatch, tmp_path):
    monkeypatch.setenv("POD_NAME", "fubala-0")
    monkeypatch.setenv("POD_UID", "uid-123")
    monkeypatch.setenv("NODE_NAME", "stor01")
    monkeypatch.setenv("FUBALA_WORKER_IMAGE_REPO", "registry.example.com/fubala/fubala-worker")
    # the per-round CLAUDE_CONFIG_DIR travels in the stdin frame, where the
    # fake cannot remap /work: point it at the fake pod root directly
    monkeypatch.setattr(pods, "CLAUDE_DIR", str(tmp_path / "pod" / ".claude"))
    pods._WARNED.clear()
    yield
    pods.set_kube_factory(None)
    pods._LIVE.clear()


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout


def story_worktree(tmp_path: Path) -> tuple[str, Path]:
    root = _init_repo(tmp_path / "proj")
    wt, _base, _new = workspaces.create(root, STORY)
    return root, Path(wt)


def make_executor(kube, lines=None) -> pods.PodExecutor:
    return pods.PodExecutor(
        STORY,
        {**SETTINGS, "run_executor": "pod"},
        story_log=(lines.append if lines is not None else None),
        kube_factory=lambda: kube,
        mode="pod",
    )


def run(ex, root, wt, config, events=None, notes=None):
    return ex.run_headless(
        "=== CODE REQUEST === implement the feature",
        config,
        (events.append if events is not None else None),
        workspace=wt,
        project_root=root,
        commit_message=f"story {STORY}: coder output",
        on_artefacts=(notes.extend if notes is not None else None),
    )


def refs(repo: str) -> dict[str, str]:
    out = git(Path(repo), "for-each-ref", "--format=%(refname) %(objectname)")
    return dict(line.split(" ", 1) for line in out.splitlines())


# ---- the exec contract --------------------------------------------------------------


def test_cli_exec_carries_neither_token_nor_prompt_and_work_comes_back(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    root, wt = story_worktree(tmp_path)
    (wt / "README.md").write_text("# demo\nlocal uncommitted edit\n")
    (wt / "notes.txt").write_text("untracked local file\n")
    start = git(wt, "rev-parse", "HEAD").strip()
    before = refs(root)
    kube = LocalPodKube(tmp_path / "pod", install_fake_claude(tmp_path, "ok"))
    lines: list[str] = []
    events: list[dict] = []
    config = make_config(artifacts_dir=str(tmp_path / "artifacts"))

    outcome = run(make_executor(kube, lines), root, wt, config, events)

    assert outcome.outcome == "ok", outcome.reason
    headless_execs = [(a, s) for a, s in kube.execs if kind_of(a) == "headless"]
    assert len(headless_execs) == 1
    argv, stdin = headless_execs[0]
    assert argv == ["sh", "-c", pods.HEADLESS_SCRIPT, "_", pods.REPO_DIR, argv[5]]
    for a, _s in kube.execs:
        assert SECRET_KEY not in " ".join(a) and "implement the feature" not in " ".join(a)
    assert SECRET_KEY.encode() in stdin and b"implement the feature" in stdin
    assert SECRET_KEY not in repr(kube.created)
    assert SECRET_KEY not in caplog.text
    assert not any(SECRET_KEY in line for line in lines)
    assert SECRET_KEY not in repr(events)
    kept = Path(outcome.transcript_path)
    assert kept.is_file() and SECRET_KEY not in kept.read_text()
    assert "[REDACTED" in kept.read_text()

    # one commit on top of start, carrying the CLI's work AND the local
    # uncommitted state; the local worktree is clean and fast-forwarded
    assert git(wt, "rev-parse", "HEAD~1").strip() == start
    assert git(wt, "log", "-1", "--format=%s").strip() == f"story {STORY}: coder output"
    files = git(wt, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(files) == ["README.md", "feature.py", "notes.txt"]
    assert git(wt, "status", "--porcelain") == ""
    assert workspaces.commit(root, STORY, f"story {STORY}: coder output") == ""
    # no snapshot commit in the branch, no ref changed but the branch
    assert "fubala snapshot" not in git(wt, "log", "--format=%s")
    after = refs(root)
    changed = {r for r in before.keys() | after.keys() if before.get(r) != after.get(r)}
    assert changed == {f"refs/heads/fubala/{STORY}"}


def test_frame_survives_awkward_values(tmp_path):
    """Newlines, quotes and a trailing newline in prompt and env values reach
    the CLI byte for byte (the frame is length-prefixed, not line-based)."""
    capture = tmp_path / "argv.txt"
    bindir = install_fake_claude(tmp_path, "capture", env={"FAKE_CLAUDE_CAPTURE": str(capture)})
    kube = LocalPodKube(tmp_path / "pod", bindir)
    root, wt = story_worktree(tmp_path)
    prompt = "line one\n'quoted' \"double\" $HOME `x`\n\n"
    ex = make_executor(kube)
    outcome = ex.run_headless(
        prompt, make_config(), None, workspace=wt, project_root=root, commit_message="m"
    )
    assert outcome.outcome == "ok", outcome.reason
    assert capture.read_text() == "\n".join(hx.child_argv(prompt, make_config())[1:])


def test_chunked_stream_relays_match_the_local_path(tmp_path):
    config = make_config()
    local_events: list[dict] = []
    bindir = install_fake_claude(tmp_path, "ok")
    local_wt = Path(_init_repo(tmp_path / "local"))
    with on_path(bindir):
        local = hx.run_code_stage(
            story_id=STORY, worktree=local_wt, prompt="go", config=config,
            on_event=local_events.append,
        )  # fmt: skip
    root, wt = story_worktree(tmp_path)
    pod_events: list[dict] = []
    kube = LocalPodKube(tmp_path / "pod", bindir, chunk=5)
    remote = run(make_executor(kube), root, wt, config, pod_events)
    assert pod_events == local_events and pod_events
    for field in ("outcome", "reason", "num_turns", "usage", "summary", "exit_code"):
        assert getattr(remote, field) == getattr(local, field), field


def test_unchanged_head_skips_the_pull(tmp_path):
    root, wt = story_worktree(tmp_path)
    before = refs(root)
    kube = LocalPodKube(tmp_path / "pod", install_fake_claude(tmp_path, "capture"))
    outcome = run(make_executor(kube), root, wt, make_config())
    assert outcome.outcome == "ok"
    pulls = [i for i, k in enumerate(kube.order) if k == "pull"]
    assert len(pulls) == 1
    assert refs(root) == before


def test_pull_bundle_refuses_when_local_head_moved(tmp_path):
    root, wt = story_worktree(tmp_path)
    start = git(wt, "rev-parse", "HEAD").strip()
    pod = tmp_path / "fakepod"
    subprocess.run(["git", "clone", "-q", str(wt), str(pod)], check=True)
    (pod / "x.txt").write_text("pod\n")
    git(pod, "add", "-A")
    git(pod, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "pod work")
    new = git(pod, "rev-parse", "HEAD").strip()
    git(pod, "bundle", "create", str(tmp_path / "b.bundle"), f"{start}..HEAD")
    bundle = (tmp_path / "b.bundle").read_bytes()
    (wt / "y.txt").write_text("local\n")
    git(wt, "add", "-A")
    git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "local moved")
    moved = git(wt, "rev-parse", "HEAD").strip()
    before = refs(root)
    with pytest.raises(workspaces.WorkspaceError, match="moved"):
        workspaces.pull_bundle(wt, STORY, bundle, start, new)
    assert git(wt, "rev-parse", "HEAD").strip() == moved
    assert refs(root) == before  # the temporary ref is gone again
    # and the happy round trip from the same bundle
    git(wt, "reset", "-q", "--hard", start)
    workspaces.pull_bundle(wt, STORY, bundle, start, new)
    assert git(wt, "rev-parse", "HEAD").strip() == new
    assert (wt / "x.txt").read_text() == "pod\n"
    assert not [r for r in refs(root) if r.startswith(workspaces.PULL_REF_PREFIX)]


def test_artefact_guard_applies_to_the_pod_commit(tmp_path):
    bindir = tmp_path / "artefact-bin"
    bindir.mkdir()
    cli = bindir / "claude"
    cli.write_text(
        "#!/bin/sh\nmkdir -p build/lib && echo x > build/lib/x.py\n"
        "echo 'V = 1' > feature.py\n"
        'echo \'{"type": "result", "result": "done", "num_turns": 1}\'\n'
    )
    cli.chmod(0o755)
    root, wt = story_worktree(tmp_path)
    kube = LocalPodKube(tmp_path / "pod", bindir)
    notes: list[str] = []
    outcome = run(make_executor(kube), root, wt, make_config(), notes=notes)
    assert outcome.outcome == "ok", outcome.reason
    assert notes == ["build/lib/x.py"]
    assert git(wt, "show", "--name-only", "--format=", "HEAD").split() == ["feature.py"]
    assert not (wt / "build").exists()  # the untracked artefact stays in the pod
    assert any(a[:3] == ["git", "-C", pods.REPO_DIR] for a, _ in kube.execs)


def test_resume_exec_reuses_the_first_config_dir_and_passes_resume(tmp_path):
    root, wt = story_worktree(tmp_path)
    capture = tmp_path / "capture.txt"
    bindir = install_fake_claude(
        tmp_path, "runtests", env={"FAKE_CLAUDE_CAPTURE": str(capture)}
    )
    kube = LocalPodKube(tmp_path / "pod", bindir)
    ex = make_executor(kube)
    first = run(ex, root, wt, make_config())
    assert first.outcome == "run_tests", first.reason
    assert first.session_id == "sess-fake-1" and first.config_dir
    second = ex.run_headless(
        "=== TEST RESULT ===",
        make_config(),
        None,
        workspace=wt,
        project_root=root,
        commit_message=f"story {STORY}: coder output",
        resume=(first.session_id, first.config_dir),
    )
    assert second.outcome == "ok", second.reason
    execs = [s for a, s in kube.execs if kind_of(a) == "headless"]
    assert len(execs) == 2
    cfg = f"CLAUDE_CONFIG_DIR {len(first.config_dir.encode())}\n{first.config_dir}".encode()
    assert cfg in execs[0] and cfg in execs[1]
    assert b"--resume" not in execs[0]
    assert b"--resume" in execs[1] and b"sess-fake-1" in execs[1]
    assert b"BASH_MAX_TIMEOUT_MS 6\n120000" in execs[1]
    assert "--resume\nsess-fake-1" in capture.read_text()


# ---- timeout, cancel, failures ------------------------------------------------------


def test_exec_timeout_kills_copies_the_transcript_then_deletes(tmp_path):
    root, wt = story_worktree(tmp_path)
    kube = LocalPodKube(tmp_path / "pod", install_fake_claude(tmp_path, "ok"))

    def hang(argv, stdin, on_output):
        raise TimeoutError(f"exec timed out; stdin {stdin!r}")

    kube.hooks["headless"] = hang
    ex = make_executor(kube)
    outcome = run(ex, root, wt, make_config(artifacts_dir=str(tmp_path / "a")))
    assert outcome.outcome == "fix" and outcome.timed_out
    tail = kube.order[kube.order.index("headless") :]
    assert tail.index("kill") < tail.index("transcript") < tail.index("delete")
    assert ex.pod_name is None


def test_in_pod_timeout_rc_124_is_the_timeout_outcome(tmp_path):
    root, wt = story_worktree(tmp_path)
    kube = LocalPodKube(tmp_path / "pod", install_fake_claude(tmp_path, "ok"))
    kube.hooks["headless"] = lambda argv, stdin, on_output: (124, "", "")
    ex = make_executor(kube)
    outcome = run(ex, root, wt, make_config())
    assert outcome.outcome == "fix" and outcome.timed_out
    assert ex.pod_name is not None  # the pod itself is healthy


def test_cancel_kills_copies_the_transcript_then_deletes(tmp_path):
    root, wt = story_worktree(tmp_path)
    start = git(wt, "rev-parse", "HEAD").strip()
    kube = LocalPodKube(tmp_path / "pod", install_fake_claude(tmp_path, "ok"))
    entered, killed = threading.Event(), threading.Event()
    session = Path(pods.CLAUDE_DIR) / "1" / "projects" / "p" / "s.jsonl"

    def block(argv, stdin, on_output):
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text(f'{{"type": "user", "text": "{SECRET_KEY}"}}\n')
        entered.set()
        killed.wait(10)
        return 143, "", ""

    def kill(argv, stdin, on_output):
        killed.set()
        return 0, "", ""

    kube.hooks.update(headless=block, kill=kill)
    ex = make_executor(kube)
    errors: list[Exception] = []

    def target():
        try:
            run(ex, root, wt, make_config(artifacts_dir=str(tmp_path / "a")))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=target, daemon=True)
    t.start()
    assert entered.wait(10)
    ex.close()
    t.join(10)
    tail = kube.order[kube.order.index("headless") :]
    assert tail[1:5] == ["kill", "transcript", "reap", "delete"], tail
    assert len(errors) == 1 and isinstance(errors[0], pods.PodRunLost)
    kept = list((tmp_path / "a").rglob("*.jsonl"))
    assert len(kept) == 1 and SECRET_KEY not in kept[0].read_text()
    assert git(wt, "rev-parse", "HEAD").strip() == start
    assert "git" not in tail and "pull" not in tail


def test_failure_before_exec_is_unavailable_after_start_is_lost(tmp_path):
    root, wt = story_worktree(tmp_path)
    bindir = install_fake_claude(tmp_path, "ok")
    failing = LocalPodKube(tmp_path / "pod1", bindir, create_error=RuntimeError("403"))
    lines: list[str] = []
    with pytest.raises(pods.PodUnavailable):
        run(make_executor(failing, lines), root, wt, make_config())
    assert len([x for x in lines if "unavailable" in x]) == 1

    kube = LocalPodKube(tmp_path / "pod2", bindir)

    def broken(argv, stdin, on_output):
        raise RuntimeError(f"websocket closed; stdin was {stdin!r}")

    kube.hooks["headless"] = broken
    start = git(wt, "rev-parse", "HEAD").strip()
    with pytest.raises(pods.PodRunLost) as info:
        run(make_executor(kube), root, wt, make_config())
    assert SECRET_KEY not in str(info.value) and "[REDACTED" in str(info.value)
    assert git(wt, "rev-parse", "HEAD").strip() == start
    assert git(wt, "status", "--porcelain") == ""


def test_pod_deadline_counts_headless_once_per_round():
    assert pods.pod_deadline(SETTINGS, "pod") == 2400 * 3 + 600 * 3
    assert pods.pod_deadline(SETTINGS, "verify") == 2400 + 600 * 3


def test_pod_env_is_claude_only():
    env = hx.pod_child_env(make_config(), "/work/.claude/3")
    assert env["CLAUDE_CONFIG_DIR"] == "/work/.claude/3"
    assert env["ANTHROPIC_AUTH_TOKEN"] == SECRET_KEY
    assert not {"PATH", "HOME", "TMPDIR"} & env.keys()
    assert {k: v for k, v in hx._child_env(make_config()).items() if k in env} == {
        k: v for k, v in env.items() if k != "CLAUDE_CONFIG_DIR"
    }


# ---- runner wiring ----------------------------------------------------------------


def _pod_client(tmp_path, monkeypatch, gateway, mode, kube_factory):
    pods.set_kube_factory(kube_factory)
    return _headless_client(
        tmp_path, monkeypatch, gateway, "ok", run_executor=mode, worker_image="20261103-1"
    )


def test_runner_runs_the_cli_in_the_pod(tmp_path, monkeypatch, gateway):
    kube = LocalPodKube(tmp_path / "pod", tmp_path / "bin-ok-0")

    def no_local(**kw):
        raise AssertionError("the round must run in the pod")

    client = _pod_client(tmp_path, monkeypatch, gateway, "pod", lambda: kube)
    monkeypatch.setattr(hx, "run_code_stage", no_local)
    try:
        sid = _run_headless_story(client, tmp_path, _seed_provider(client), "In pod")
        final = _wait_for_status(client, sid, "human_review", 120)
        assert not final.get("error"), final
        assert "headless" in kube.order
        assert SECRET_KEY not in repr(kube.created)
        wt = Path(
            next(e for e in _events(client, sid, "note") if e["data"].get("worktree"))[
                "data"
            ]["worktree"]
        )
        assert "feature.py" in git(wt, "ls-files")
        assert _events(client, sid, "headless_stream")
        _no_key_anywhere(client, sid)
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("graph", [False, True])
def test_runner_pod_round_carries_the_graph_only_when_the_setting_is_on(
    tmp_path, monkeypatch, gateway, graph
):
    """Off: no --mcp-config and no graph-build wrapper in the coder exec argv.
    On (positive control): both."""
    kube = LocalPodKube(tmp_path / "pod", tmp_path / "bin-ok-0")
    client = _pod_client(tmp_path, monkeypatch, gateway, "pod", lambda: kube)
    # the review's impact report runs locally: no real code-review-graph binary
    monkeypatch.setattr(code_graph, "build", lambda wt, out: None)
    try:
        model = _seed_provider(client)
        proj = client.post(
            "/api/v1/projects",
            json={
                "name": "demo", "root": _init_repo(tmp_path / "repo-dir"),
                "code_graph_enabled": graph,
            },
        ).json()
        story = client.post(
            "/api/v1/stories",
            json={
                "title": "In pod", "project_id": proj["id"],
                "execution": "headless", "model": model,
            },
        ).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        _wait_for_status(client, story["id"], "human_review", 60)
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and "headless" not in kube.order:
            time.sleep(0.1)
        assert "headless" in kube.order
        frames = [s for a, s in kube.execs if kind_of(a) == "headless"]
        _env, argv = parse_frame(frames[0])
        assert "claude" in " ".join(argv)
        assert ("--mcp-config" in argv) is graph
        assert (argv[:2] == ["sh", "-c"] and "CRG_DATA_DIR" in argv[2]) is graph
    finally:
        client.__exit__(None, None, None)


def _requeue_events(client, sid):
    return [
        e
        for e in _events(client, sid, "status")
        if e["data"].get("pod_lost_requeue")
    ]


def _requeue_notes(client, sid):
    notes = client.get(f"/api/v1/stories/{sid}").json()["notes"]
    return [n for n in notes if "worker pod lost mid-run" in n["body"]]


def test_runner_exec_lost_requeues_once_then_fails_infrastructure(
    tmp_path, monkeypatch, gateway
):
    kube = LocalPodKube(tmp_path / "pod", tmp_path / "bin-ok-0")
    calls: list[int] = []

    def broken(argv, stdin, on_output):
        calls.append(1)
        raise RuntimeError(f"stream reset {stdin!r}")

    kube.hooks["headless"] = broken

    local_calls: list[dict] = []
    client = _pod_client(tmp_path, monkeypatch, gateway, "pod", lambda: kube)
    monkeypatch.setattr(hx, "run_code_stage", lambda **kw: local_calls.append(kw))
    try:
        sid = _run_headless_story(client, tmp_path, _seed_provider(client), "Lost")
        final = _wait_for_status(client, sid, "failed", 120)
        assert final["error"].startswith("infrastructure: worker pod lost mid-run: ")
        assert SECRET_KEY not in final["error"]
        assert len(calls) == 2
        assert local_calls == []
        assert len(_requeue_events(client, sid)) == 1
        assert len(_requeue_notes(client, sid)) == 1
        _no_key_anywhere(client, sid)
    finally:
        client.__exit__(None, None, None)


def test_runner_exec_lost_once_requeues_and_the_rerun_succeeds(
    tmp_path, monkeypatch, gateway
):
    kube = LocalPodKube(tmp_path / "pod", tmp_path / "bin-ok-0")

    def broken_once(argv, stdin, on_output):
        del kube.hooks["headless"]
        raise RuntimeError("stream reset")

    kube.hooks["headless"] = broken_once
    client = _pod_client(tmp_path, monkeypatch, gateway, "pod", lambda: kube)
    try:
        sid = _run_headless_story(client, tmp_path, _seed_provider(client), "Lost once")
        final = _wait_for_status(client, sid, "human_review", 120)
        assert not final.get("error"), final
        events = _requeue_events(client, sid)
        assert len(events) == 1 and events[0]["data"]["to"] == "queued"
        notes = _requeue_notes(client, sid)
        assert len(notes) == 1
        assert re.search(r"worker pod lost mid-run: .*; requeued 1/1", notes[0]["body"])
    finally:
        client.__exit__(None, None, None)


def test_runner_cancel_during_a_pod_run_is_never_requeued(tmp_path, monkeypatch, gateway):
    kube = LocalPodKube(tmp_path / "pod", tmp_path / "bin-ok-0")
    entered, release = threading.Event(), threading.Event()

    def block(argv, stdin, on_output):
        entered.set()
        release.wait(30)
        return 143, "", ""

    kube.hooks["headless"] = block
    client = _pod_client(tmp_path, monkeypatch, gateway, "pod", lambda: kube)
    try:
        sid = _run_headless_story(client, tmp_path, _seed_provider(client), "Cancel")
        assert entered.wait(60)
        resp = client.post(f"/api/v1/stories/{sid}/cancel", json={"by": "t"})
        assert resp.status_code < 300, resp.text
        release.set()
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            story = client.get(f"/api/v1/stories/{sid}").json()
            assert story["status"] == "failed", story
            assert story["error"].startswith("cancelled by"), story
            time.sleep(0.1)
        assert _requeue_events(client, sid) == []
        assert _requeue_notes(client, sid) == []
    finally:
        release.set()
        client.__exit__(None, None, None)


def test_pod_lost_requeue_count_survives_a_restart(tmp_path, monkeypatch, gateway):
    kube = LocalPodKube(tmp_path / "pod", tmp_path / "bin-ok-0")
    calls: list[int] = []

    def broken(argv, stdin, on_output):
        calls.append(1)
        raise RuntimeError("stream reset")

    kube.hooks["headless"] = broken
    client = _pod_client(tmp_path, monkeypatch, gateway, "pod", lambda: kube)
    proj = client.post(
        "/api/v1/projects",
        json={"name": "demo", "root": _init_repo(tmp_path / "repo-dir")},
    ).json()
    story = client.post(
        "/api/v1/stories",
        json={
            "title": "Restart",
            "project_id": proj["id"],
            "execution": "headless",
            "model": _seed_provider(client),
        },
    ).json()
    sid = story["id"]
    client.post(f"/api/v1/stories/{sid}/queue")
    _wait_for_status(client, sid, "human_review", 60)
    client.__exit__(None, None, None)

    db = tmp_path / "headless-ok.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO story_events (story_id, kind, data, created_at)"
        " VALUES (?, 'status', ?, ?)",
        (
            sid,
            json.dumps({"to": "queued", "pod_lost_requeue": True, "attempt": 1}),
            "2026-11-03 00:00:00",
        ),
    )
    conn.commit()
    conn.close()

    from fastapi.testclient import TestClient

    from app.main import create_app

    fresh = TestClient(create_app(gateway=gateway))
    fresh.__enter__()
    try:
        fresh.post(f"/api/v1/stories/{sid}/approve", json={"by": "t"})
        final = _wait_for_status(fresh, sid, "failed", 120)
        assert final["error"].startswith("infrastructure: worker pod lost mid-run: ")
        assert len(calls) == 1
    finally:
        fresh.__exit__(None, None, None)


def test_runner_create_failure_runs_the_round_locally_with_one_warning(
    tmp_path, monkeypatch, gateway, caplog
):
    caplog.set_level(logging.WARNING)
    kube = LocalPodKube(tmp_path / "pod", tmp_path / "bin-ok-0", create_error=RuntimeError("403"))
    client = _pod_client(tmp_path, monkeypatch, gateway, "pod", lambda: kube)
    try:
        sid = _run_headless_story(client, tmp_path, _seed_provider(client), "Local")
        final = _wait_for_status(client, sid, "human_review", 120)
        assert not final.get("error"), final
        assert "headless" not in kube.order and not kube.created
        warnings = [r for r in caplog.records if "worker pod fallback" in r.getMessage()]
        assert len(warnings) == 1
    finally:
        client.__exit__(None, None, None)


def test_runner_verify_mode_keeps_headless_local(tmp_path, monkeypatch, gateway):
    kube = LocalPodKube(tmp_path / "pod", tmp_path / "bin-ok-0")
    client = _pod_client(tmp_path, monkeypatch, gateway, "verify", lambda: kube)
    try:
        sid = _run_headless_story(client, tmp_path, _seed_provider(client), "Verify")
        final = _wait_for_status(client, sid, "human_review", 120)
        assert not final.get("error"), final
        rounds = [e for e in _events(client, sid, "round") if e["data"].get("executor")]
        assert rounds and rounds[0]["data"]["outcome"] == "ok"
        assert "headless" not in kube.order  # the pod (lazy) serves verify only
    finally:
        client.__exit__(None, None, None)


# ---- end-of-run reaping ---------------------------------------------------------------


@pytest.fixture
def reports():
    got: list[tuple[str, str, list]] = []
    procs.set_reporter(lambda sid, reason, entries: got.append((sid, reason, entries)))
    yield got
    procs.set_reporter(None)


def test_headless_round_reaps_before_the_commit_and_reports_redacted(tmp_path, reports):
    root, wt = story_worktree(tmp_path)
    kube = LocalPodKube(tmp_path / "pod", install_fake_claude(tmp_path, "ok"))
    kube.hooks["reap"] = lambda argv, stdin, on_output: (
        0,
        f"77\t30\tsleep\tcurl {SECRET_KEY}\n",
        "",
    )
    ex = make_executor(kube)
    outcome = run(ex, root, wt, make_config(artifacts_dir=str(tmp_path / "a")))
    assert outcome.outcome == "ok", outcome.reason
    tail = kube.order[kube.order.index("headless") :]
    assert tail.index("transcript") < tail.index("reap") < tail.index("git")
    assert len(reports) == 1 and reports[0][:2] == (STORY, "round_end")
    entry = reports[0][2][0]
    assert entry["pid"] == 77 and entry["age_s"] == 30
    assert SECRET_KEY not in repr(reports)


def test_headless_timeout_reports_the_timeout_reason(tmp_path, reports):
    root, wt = story_worktree(tmp_path)
    kube = LocalPodKube(tmp_path / "pod", install_fake_claude(tmp_path, "ok"))
    kube.hooks["headless"] = lambda argv, stdin, on_output: (124, "", "")
    kube.hooks["reap"] = lambda argv, stdin, on_output: (0, "5\t1\tsleep\tsleep\n", "")
    outcome = run(make_executor(kube), root, wt, make_config())
    assert outcome.timed_out
    assert [r[1] for r in reports] == ["timeout"]


@pytest.mark.parametrize("failure", ["raises", "rc", "garbage"])
def test_a_failing_reap_does_not_change_the_round(tmp_path, reports, failure):
    root, wt = story_worktree(tmp_path)
    kube = LocalPodKube(tmp_path / "pod", install_fake_claude(tmp_path, "ok"))

    def reap(argv, stdin, on_output):
        if failure == "raises":
            raise RuntimeError("stream reset")
        return (1, "", "") if failure == "rc" else (0, "not\ta\tproc\tline\n\x00", "")

    kube.hooks["reap"] = reap
    outcome = run(make_executor(kube), root, wt, make_config())
    assert outcome.outcome == "ok", outcome.reason
    assert reports == []
    assert git(wt, "log", "-1", "--format=%s").strip() == f"story {STORY}: coder output"

