"""Run transparency: persisted activity events, uncapped diff endpoint,
and the budget/progress payloads the frontend meters ride on.

Covers the spec's acceptance checks:
1. A coder run persists StoryEvent rows in the order
   round → tool_call → tool_result → round… and they survive a fresh
   GET /stories/{id} (the after-refresh replay path).
2. GET /api/v1/stories/{id}/diff serves the UNCAPPED worktree diff — a
   marker past the 60k code_diff cap is visible — and 404s clearly when
   the worktree is missing.
3. run_agent's on_progress reaches tool_calls == max_tool_calls with
   monotonically increasing round numbers (threshold states for meters).
"""

import json
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app import workspaces
from app.agent import MAX_ROUNDS
from app.main import create_app
from app.runner import ACTIVITY_KINDS

from conftest import MINIMAL_SPEC


def init_repo(tmp_path) -> str:
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "README.md").write_text("# demo repo\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return str(repo)


def wait_for_status(client: TestClient, story_id: str, statuses: tuple[str, ...], timeout: float = 30):
    deadline = time.time() + timeout
    cur = None
    while time.time() < deadline:
        cur = client.get(f"/api/v1/stories/{story_id}").json()
        if cur["status"] in statuses:
            return cur
        time.sleep(0.05)
    raise AssertionError(f"story never reached {statuses}: {cur and cur.get('error')}")


def _import_providers():
    from app.providers import Delta as D
    from app.providers import ToolCall as TC
    from app.providers import Usage as U

    return D, TC, U


def activity_from(detail: dict) -> list[tuple[str, dict]]:
    out = []
    for ev in detail["events"]:
        if ev["kind"] in ACTIVITY_KINDS:
            out.append((ev["kind"], json.loads(ev["data"])))
    return out


class ActivityGateway:
    """Spec → coder: coder calls a tool twice, then answers."""

    def __init__(self):
        self.calls = 0

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
        D, TC, U = _import_providers()
        self.calls += 1
        if self.calls == 1:  # spec writer
            yield D(text=MINIMAL_SPEC)
        elif self.calls == 2:  # spec ai-review
            yield D(text="Spec looks good.\n\nVERDICT: APPROVED")
        elif self.calls == 3:  # coder round 1: tool
            yield TC(id="t1", name="write_file", arguments=json.dumps({
                "path": "feature.py", "content": "VALUE = 1\n",
            }))
        elif self.calls == 4:  # coder round 2: second tool
            yield TC(id="t2", name="write_file", arguments=json.dumps({
                "path": "other.py", "content": "OTHER = 2\n",
            }))
        else:  # coder final answer
            yield D(text="Done.")
        yield U(prompt_tokens=5, completion_tokens=5)


def test_coder_run_persists_round_tool_events_in_order(tmp_path, monkeypatch, legacy_review):
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "activity.db"))

    with TestClient(create_app(gateway=ActivityGateway())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "tools_max_calls": 8,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "activity story"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        wait_for_status(client, story["id"], ("human_review", "failed"))
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        final = wait_for_status(client, story["id"], ("human_review", "failed"))
        assert final["status"] == "human_review", final.get("error")

        # Fresh GET: the activity events survive a refresh (replay path).
        detail = client.get(f"/api/v1/stories/{story['id']}").json()
        acts = activity_from(detail)
        kinds = [k for k, _ in acts]
        assert kinds, "no activity events persisted"
        assert set(kinds) == {"round", "tool_call", "tool_result"}, kinds

        # Two tools → round, call, result, round, call, result, round…
        seq = [k for k in kinds if k != "round"]
        assert seq == ["tool_call", "tool_result", "tool_call", "tool_result"], kinds

        # Tool events carry name + a capped preview; path is extracted.
        calls = [d for k, d in acts if k == "tool_call"]
        assert [c["name"] for c in calls] == ["write_file", "write_file"]
        assert calls[0]["path"] == "feature.py"
        assert len(calls[0]["args"]) <= 80

        # Round events carry the budget snapshot, rounds monotonic.
        # (the code review's own session reports rounds too, as phase code_review)
        rounds = [d for k, d in acts if k == "round" and d.get("phase") != "code_review"]
        nums = [r["round"] for r in rounds]
        assert nums == sorted(nums), nums
        assert nums[-1] >= 3
        assert all(r["max_tool_calls"] == 8 for r in rounds)
        assert all("prompt_tokens" in r and "completion_tokens" in r for r in rounds)
        # tokens are cumulative across rounds
        assert rounds[-1]["prompt_tokens"] >= rounds[0]["prompt_tokens"]

        # span events bracket each tool_result
        order = [
            ev["kind"]
            for ev in detail["events"]
            if ev["kind"] in ("tool_started", "tool_result", "tool_finished")
        ]
        assert order == ["tool_started", "tool_result", "tool_finished"] * 2, order
        started = [
            json.loads(ev["data"]) for ev in detail["events"] if ev["kind"] == "tool_started"
        ]
        for st in started:
            assert st["name"] == "write_file"
            assert st["call_id"] and st["started_at"].endswith("Z")
            assert st["timeout_s"] > 0

        # tool_result status heuristic: write_file results are plain text → ok
        results = [d for k, d in acts if k == "tool_result"]
        assert all(d["status"] in ("ok", "error", "truncated") for d in results)
        assert any(d["status"] == "ok" for d in results)


def test_tool_activity_helpers_cap_previews():
    from app.runner import tool_call_activity, tool_result_activity

    long_args = json.dumps({"path": "x" * 300})
    call = tool_call_activity("write_file", long_args)
    assert len(call["args"]) <= 80
    assert call["path"].startswith("x")

    ok = tool_result_activity("plain text output")
    assert ok["status"] == "ok"
    assert ok["chars"] == len("plain text output")

    err = tool_result_activity(json.dumps({"error": "boom"}))
    assert err["status"] == "error"
    assert err["error"] == "boom"

    trunc = tool_result_activity("y" * 21_000)
    assert trunc["status"] == "truncated"


def test_diff_endpoint_serves_uncapped_diff_and_404s(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "diff.db"))
    marker = "UNIQUE_MARKER_PAST_60K_BOUNDARY"

    with TestClient(create_app(gateway=ActivityGateway())) as client:
        client.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": repo,
                "write_tools_enabled": True,
                "tools_max_calls": 8,
            },
        )
        story = client.post("/api/v1/stories", json={"title": "big diff"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")
        wait_for_status(client, story["id"], ("human_review", "failed"))
        client.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})
        final = wait_for_status(client, story["id"], ("human_review", "failed"))
        assert final["status"] == "human_review", final.get("error")

        # Oversize the worktree diff past the 60k code_diff cap.
        wt = Path(repo) / ".fubala-worktrees" / story["id"]
        assert wt.exists(), f"worktree missing at {wt}"
        (wt / "large.py").write_text("X" * 120 + "\n" * 700 + "Y" * 70_000 + "\n" + marker + "\n")

        detail = client.get(f"/api/v1/stories/{story['id']}").json()
        capped = detail["code_diff"]
        assert capped and "[... diff truncated" in capped
        assert marker not in capped

        r = client.get(f"/api/v1/stories/{story['id']}/diff")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/plain")
        assert marker in r.text
        assert "[... diff truncated" not in r.text
        assert "diff --git" in r.text

        # ?download=1 sets the attachment header
        rd = client.get(f"/api/v1/stories/{story['id']}/diff?download=1")
        assert rd.status_code == 200
        assert rd.headers["content-disposition"] == (
            f'attachment; filename="{story["id"]}.diff"'
        )
        assert marker in rd.text

        # Unknown story → 404
        assert client.get("/api/v1/stories/nope/diff").status_code == 404

        # Missing worktree → 404 with a clear message
        import shutil

        shutil.rmtree(wt, ignore_errors=True)
        missing = client.get(f"/api/v1/stories/{story['id']}/diff")
        assert missing.status_code == 404
        assert "worktree" in missing.json()["detail"] or "diff" in missing.json()["detail"]


def test_diff_endpoint_404_before_execution(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "diff2.db"))

    with TestClient(create_app(gateway=ActivityGateway())) as client:
        client.patch(
            "/api/v1/settings", json={"story_poll_interval": 0.05, "project_root": repo}
        )
        story = client.post("/api/v1/stories", json={"title": "not run yet"}).json()
        r = client.get(f"/api/v1/stories/{story['id']}/diff")
        assert r.status_code == 404
        assert "no diff yet" in r.json()["detail"]


def test_on_progress_hits_max_tool_calls_threshold(tmp_path):
    """The progress payload must reach tool_calls == max_tool_calls so the
    meters can show exhausted, and round must be monotonic."""
    import asyncio

    from app.agent import run_agent

    D, TC, U = _import_providers()

    class ToolLoopGateway:
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            self.calls += 1
            if tools:
                yield TC(id=f"c{self.calls}", name="noop", arguments="{}")
            else:
                yield D(text="forced final answer")
            yield U(prompt_tokens=3, completion_tokens=4)

    seen: list[dict] = []

    async def main():
        await run_agent(
            ToolLoopGateway(),
            model="m",
            messages=[{"role": "user", "content": "go"}],
            tools=[{"type": "function", "function": {"name": "noop", "parameters": {}}}],
            execute=lambda name, args: "ok",
            on_progress=seen.append,
            max_tool_calls=3,
            timeout_seconds=60,
        )

    asyncio.run(main())

    assert seen, "on_progress never fired"
    assert [p["round"] for p in seen] == sorted(p["round"] for p in seen)
    assert seen[0]["tool_calls"] == 0
    assert seen[-1]["tool_calls"] == 3, seen[-1]
    assert seen[-1]["tool_calls"] == 3, seen[-1]
    assert max(p["tool_calls"] for p in seen) == 3
    assert all(p["max_tool_calls"] == 3 for p in seen)
    assert all(p["max_rounds"] == MAX_ROUNDS for p in seen)
    assert all(p["prompt_tokens"] == 3 * (p["round"] - 1) for p in seen), (
        "usage must be cumulative across completed rounds"
    )
    assert all(p["elapsed_s"] >= 0 and p["timeout_s"] == 60 for p in seen)


def test_on_progress_cumulative_usage(tmp_path):
    import asyncio

    from app.agent import run_agent

    D, TC, U = _import_providers()

    class OneRoundGateway:
        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            yield U(prompt_tokens=100, completion_tokens=50)
            yield D(text="answer")

    seen: list[dict] = []

    async def main():
        await run_agent(
            OneRoundGateway(),
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            on_progress=seen.append,
        )

    asyncio.run(main())
    assert seen and seen[0]["round"] == 1
    assert seen[0]["prompt_tokens"] == 0  # fires BEFORE the round completes
    assert seen[0]["max_tool_calls"] is None
    assert seen[0]["timeout_s"] is None


def test_run_agent_without_on_progress_is_unchanged():
    import asyncio

    from app.agent import run_agent

    D, TC, U = _import_providers()

    class G:
        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            yield U(prompt_tokens=1, completion_tokens=1)
            yield D(text="plain")

    async def main():
        result = await run_agent(G(), model="m", messages=[{"role": "user", "content": "hi"}])
        assert result.text == "plain"
        assert result.rounds == 1
        assert result.tool_calls == 0

    asyncio.run(main())


def test_span_hooks_pick_timeout_per_tool(tmp_path, monkeypatch):
    from app.runner import activity_tool_span_hooks

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "span.db"))
    with TestClient(create_app(gateway=ActivityGateway())) as client:
        story = client.post("/api/v1/stories", json={"title": "span"}).json()
        start, end = activity_tool_span_hooks(client.app.state.engine, story["id"], 30, 900)
        start("a", "run_verify", "{}")
        start("b", "read_file", "{}")
        end("a")
        events = client.get(f"/api/v1/stories/{story['id']}").json()["events"]
        started = {
            d["call_id"]: d
            for d in (json.loads(e["data"]) for e in events if e["kind"] == "tool_started")
        }
        assert started["a"]["timeout_s"] == 900
        assert started["b"]["timeout_s"] == 30
        assert any(e["kind"] == "tool_finished" for e in events)


def test_run_agent_tool_span_callbacks():
    import asyncio

    from app.agent import run_agent

    D, TC, U = _import_providers()

    class G:
        def __init__(self):
            self.n = 0

        async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None):
            self.n += 1
            if self.n == 1:
                yield TC(id="", name="slow", arguments="{}")
            else:
                yield D(text="done")
            yield U(prompt_tokens=1, completion_tokens=1)

    async def scenario(execute):
        log: list[str] = []
        await run_agent(
            G(),
            model="m",
            messages=[{"role": "user", "content": "go"}],
            tools=[{"type": "function", "function": {"name": "slow", "parameters": {}}}],
            execute=execute,
            on_tool_start=lambda cid, name, args: log.append(f"start:{cid}"),
            on_tool=lambda *a: log.append("tool"),
            on_tool_end=lambda cid: log.append(f"end:{cid}"),
        )
        return log

    async def slow(name, args):
        await asyncio.sleep(0.2)
        return "ok"

    assert asyncio.run(scenario(slow)) == ["start:r1-0", "tool", "end:r1-0"]

    log: list[str] = []

    def boom(name, args):
        raise RuntimeError("x")

    async def failing():
        try:
            await run_agent(
                G(),
                model="m",
                messages=[{"role": "user", "content": "go"}],
                tools=[{"type": "function", "function": {"name": "slow", "parameters": {}}}],
                execute=boom,
                on_tool_start=lambda cid, name, args: log.append("start"),
                on_tool_end=lambda cid: log.append("end"),
            )
        except RuntimeError:
            pass

    asyncio.run(failing())
    assert log == ["start", "end"]
