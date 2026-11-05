"""wait_for_story progress notifications (MCP Context wiring).

The tool keeps its HTTP-facing contract byte-identical; the only new behaviour
is progress ticks / status-change info lines pushed through the SDK Context
object, which must never surface failures in the return string. Calls with no
ctx (direct coroutines, as the rest of the suite does) keep working.

Uses the same direct-call mechanics as test_external_execution.py's
_mcp_setup: monkeypatch app.mcp_tools._get, then asyncio.run the tool.
"""

import asyncio


class FakeContext:
    """Stands in for mcp.server.mcpserver.Context: records every notification
    instead of shipping it, and can be told to blow up to prove failures are
    swallowed."""

    def __init__(self, fail: bool = False):
        self.progress: list[tuple[float, float | None]] = []
        self.messages: list[str] = []
        self.fail = fail

    async def report_progress(self, progress: float, total: float | None = None):
        if self.fail:
            raise RuntimeError("client advertised no progress capability")
        self.progress.append((progress, total))

    async def info(self, data, **_):
        if self.fail:
            raise RuntimeError("notification channel dead")
        self.messages.append(str(data))


def _story(status: str) -> dict:
    return {
        "id": "story-1",
        "title": "Wait on me",
        "status": status,
        "depends_on": [],
        "spec_version": 0,
    }


def _setup(monkeypatch, statuses):
    """Point app.mcp at a scripted /stories/{id} that yields `statuses` in
    order and then repeats the last one forever."""
    from app import mcp_tools as mcp

    calls = {"n": 0}

    async def scripted_get(path):
        assert path == "/stories/story-1"
        i = min(calls["n"], len(statuses) - 1)
        calls["n"] += 1
        return _story(statuses[i])

    monkeypatch.setattr(mcp, "_get", scripted_get)
    return mcp, calls


def test_reached_target_reports_progress_and_info(monkeypatch):
    mcp, _ = _setup(monkeypatch, ["in_progress", "in_progress", "done"])
    ctx = FakeContext()
    # two waiting polls (1s sleep each) before the target lands on poll 3
    out = asyncio.run(mcp.wait_for_story("story-1", timeout_seconds=30, ctx=ctx))
    assert out.startswith("reached done\n")
    assert len(ctx.progress) == 2  # one tick per waiting poll, none past the end
    assert all(total == 30 for _, total in ctx.progress)
    elapsed = [e for e, _ in ctx.progress]
    assert elapsed == sorted(elapsed)
    assert ctx.messages == [
        "still waiting: story in in_progress (0s/30s)"
    ]  # first observed status logs once; unchanged status stays silent
    assert out.splitlines()[0] == "reached done"


def test_timeout_reports_each_status_change(monkeypatch):
    mcp, _ = _setup(monkeypatch, ["queued", "in_progress", "spec"])
    ctx = FakeContext()
    # deliberately short: this test asserts the timeout message itself, so the
    # wait must time out
    out = asyncio.run(mcp.wait_for_story("story-1", timeout_seconds=3, ctx=ctx))
    assert out.startswith("timeout after 3s — still spec\n")
    # one info line per status change, including the first observed status
    assert ctx.messages == [
        "still waiting: story in queued (0s/3s)",
        "still waiting: story in in_progress (1s/3s)",
        "still waiting: story in spec (2s/3s)",
    ]
    # a tick on every poll, with the fixed total and non-decreasing elapsed
    assert len(ctx.progress) == 3
    assert all(total == 3 for _, total in ctx.progress)
    elapsed = [e for e, _ in ctx.progress]
    assert elapsed == sorted(elapsed)
    assert all(0 <= e <= 3 for e in elapsed)


def test_timeout_deadline_tick_not_past_deadline(monkeypatch):
    """The final poll that hits the deadline returns immediately: no tick for
    it, so progress never claims elapsed > total."""
    mcp, _ = _setup(monkeypatch, ["queued"])
    ctx = FakeContext()
    # deliberately short: this test asserts the timeout message itself, so the
    # wait must time out
    out = asyncio.run(mcp.wait_for_story("story-1", timeout_seconds=1, ctx=ctx))
    assert out.startswith("timeout after 1s — still queued\n")
    assert all(e <= 1 for e, _ in ctx.progress)
    assert ctx.messages == ["still waiting: story in queued (0s/1s)"]


def test_no_ctx_still_returns(monkeypatch):
    mcp, _ = _setup(monkeypatch, ["in_progress", "done"])
    out = asyncio.run(mcp.wait_for_story("story-1", timeout_seconds=30))
    assert out.startswith("reached done\n")


def test_broken_context_never_aborts(monkeypatch):
    """A client that rejects progress/logs (or a dead channel) must not change
    the return or break the loop."""
    mcp, _ = _setup(monkeypatch, ["queued", "in_progress", "done"])
    ctx = FakeContext(fail=True)
    out = asyncio.run(mcp.wait_for_story("story-1", timeout_seconds=30, ctx=ctx))
    assert out.startswith("reached done\n")
    assert ctx.progress == [] and ctx.messages == []


def test_reached_on_first_poll_has_no_ticks(monkeypatch):
    """Target already met: return before any notification is attempted."""
    mcp, _ = _setup(monkeypatch, ["done"])
    ctx = FakeContext()
    out = asyncio.run(mcp.wait_for_story("story-1", timeout_seconds=30, ctx=ctx))
    assert out.startswith("reached done\n")
    assert ctx.progress == [] and ctx.messages == []
