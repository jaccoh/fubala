"""Concurrency as slots, not stories.

Two pools: model_slots caps LLM calls that are generating, test_slots caps
verify runs. Any number of stories is in flight; a story holds a model slot
only while a model call runs. Every test here drives the real call sites
(spec writer, coder round, headless run, verify, reviewers) with fake
providers, and every wait is event-based: a pool change, a fake call entering
or leaving, or a bus event pokes the waiter, which re-checks its condition.
No sleep-plus-deadline polling.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import MINIMAL_SPEC
from sqlmodel import Session, select
from test_execution import init_repo, phase_of

from app import headless as headless_exec
from app import pods
from app import runner as runner_mod
from app.config import Settings
from app.db import init_db, make_engine
from app.events import bus
from app.models import Story, StoryStatus
from app.providers import Delta, GatewayError, ToolCall, Usage
from app.registry import ProviderRegistry
from app.runner import SlotPool, StoryRunner
from app.settings_store import save_settings

SPEC = "=== SPEC REQUEST ==="
CODE = "=== CODE REQUEST ==="
AI_REVIEW = "=== AI REVIEW REQUEST ==="
CODE_REVIEW = "=== CODE REVIEW REQUEST ==="
WAIT = 30.0  # guard only: every wait below returns on an event


class Signal:
    """One wake-up flag for `until`: anything that may change a condition
    pokes it (from another thread through the loop)."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.event = asyncio.Event()
        self.loop = loop

    def poke(self) -> None:
        self.event.set()

    def poke_threadsafe(self) -> None:
        assert self.loop is not None
        self.loop.call_soon_threadsafe(self.event.set)

    async def until(self, cond, timeout: float = WAIT) -> None:
        async def wait() -> None:
            while True:
                self.event.clear()
                if cond():
                    return
                await self.event.wait()

        await asyncio.wait_for(wait(), timeout)


def watch(runner: StoryRunner, sig: Signal) -> None:
    """Poke `sig` on every grant, queue and release of both pools."""
    for pool in (runner._model_pool, runner._test_pool):
        inner = pool.on_change

        def hook(inner=inner) -> None:
            if inner is not None:
                inner()
            sig.poke()

        pool.on_change = hook


class SlotGateway:
    """Fake provider: counts overlapping calls (the peak counter), blocks the
    phases in `block` until `release` is set, raises a 429 for prompts that
    name a title in `fail_429`."""

    def __init__(self, block: tuple[str, ...] = ()) -> None:
        self.block = block
        self.release = asyncio.Event()
        # reviewer phases (AI review, code review) block here when set
        self.review_gate: asyncio.Event | None = None
        self.gates: dict[str, asyncio.Event] = {}
        self.fail_429: set[str] = set()
        self.active = 0
        self.peak = 0
        self.entered: list[tuple[str | None, str]] = []
        self.sig: Signal | None = None

    def _poke(self) -> None:
        if self.sig is not None:
            self.sig.poke()

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None, **kw):
        phase = phase_of(messages)
        text = "\n".join(m["content"] for m in messages if isinstance(m.get("content"), str))
        title = next((t for t in self.gates if t in text), "")
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.entered.append((phase, title or text[:80]))
        self._poke()
        try:
            if title:
                await self.gates[title].wait()
            elif phase in self.block:
                await self.release.wait()
            elif phase in (AI_REVIEW, CODE_REVIEW) and self.review_gate is not None:
                await self.review_gate.wait()
            if any(t in text for t in self.fail_429):
                raise GatewayError("429 Too Many Requests: rate limit exceeded")
            if phase == SPEC:
                yield Delta(text=MINIMAL_SPEC)
            elif phase == CODE and messages[-1].get("role") != "tool":
                # the coder's one change, then "done" after the tool result
                name = f"out-{abs(hash(text)) % 10**8}.txt"
                yield ToolCall(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps({"path": name, "content": "x\n"}),
                )
            elif phase == CODE:
                yield Delta(text="done")
            else:
                yield Delta(text="looks right\n\nVERDICT: APPROVED")
            yield Usage(prompt_tokens=1, completion_tokens=1)
        finally:
            self.active -= 1
            self._poke()

    def calls(self, phase: str) -> int:
        return sum(1 for p, _ in self.entered if p == phase)


def make_runner(tmp_path: Path, gateway, **settings) -> StoryRunner:
    """A runner whose scheduler loop is NOT started: the tests drive the
    pipeline entry points themselves."""
    engine = make_engine(Settings(database_path=str(tmp_path / "slots.db")))
    init_db(engine)
    registry = ProviderRegistry(engine)
    registry.seed_if_empty()
    if settings:
        save_settings(engine, settings)
    return StoryRunner(engine, gateway, registry=registry)


def add_story(runner: StoryRunner, title: str, status: str, **fields) -> str:
    with Session(runner.engine) as db:
        story = Story(title=title, status=status, model="echo/dev", **fields)
        db.add(story)
        db.commit()
        return story.id


def status_of(runner: StoryRunner, story_id: str) -> str:
    with Session(runner.engine) as db:
        return db.get(Story, story_id).status


def coder_settings(tmp_path: Path, **extra) -> dict:
    return {
        "project_root": init_repo(tmp_path),
        "write_tools_enabled": True,
        "verify_command": "",
        **extra,
    }


def launch_coders(runner: StoryRunner, story_ids: list[str]) -> None:
    """The real pickup path for executing stories (_tick's launch)."""
    with Session(runner.engine) as db:
        for sid in story_ids:
            runner._launch(runner_mod.story_payload(db.get(Story, sid)), True)


async def drain(runner: StoryRunner) -> None:
    tasks = list(runner._tasks.values())
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), WAIT)


# ---- the pool ---------------------------------------------------------------


def test_grants_follow_the_order_of_acquire_calls():
    async def scenario() -> list[str]:
        sig = Signal()
        pool = SlotPool("model", 1, sig.poke)
        await pool.acquire("a")
        granted: list[str] = []

        async def take(sid: str) -> None:
            await pool.acquire(sid)
            granted.append(sid)
            sig.poke()

        tasks = []
        for sid in ("d", "b", "c"):
            tasks.append(asyncio.create_task(take(sid)))
            # this request is queued before the next one asks
            await sig.until(lambda sid=sid: sid in pool.waiting())
        assert pool.waiting() == ["d", "b", "c"]
        # a request that asks now queues behind all three, even though it
        # would be "first" by name
        late = asyncio.create_task(take("0-late"))
        await sig.until(lambda: "0-late" in pool.waiting())
        for n, holder in enumerate(("a", "d", "b", "c"), 1):
            pool.release(holder)
            await sig.until(lambda n=n: len(granted) == n)
            assert len(pool.holders()) == 1
        await asyncio.gather(*tasks, late)
        return granted

    assert asyncio.run(scenario()) == ["d", "b", "c", "0-late"]


def test_shrinking_never_interrupts_a_holder_and_only_blocks_new_grants():
    async def scenario() -> None:
        sig = Signal()
        pool = SlotPool("model", 2, sig.poke)
        await pool.acquire("a")
        await pool.acquire("b")
        pool.resize(1)
        assert pool.holders() == ["a", "b"]  # nobody is interrupted
        waiter = asyncio.create_task(pool.acquire("c"))
        await sig.until(lambda: pool.waiting() == ["c"])
        pool.release("a")
        assert pool.holders() == ["b"] and pool.waiting() == ["c"]
        pool.release("b")
        await asyncio.wait_for(waiter, WAIT)
        assert pool.holders() == ["c"]
        # growing grants at once
        nxt = asyncio.create_task(pool.acquire("d"))
        await sig.until(lambda: pool.waiting() == ["d"])
        pool.resize(2)
        await asyncio.wait_for(nxt, WAIT)
        assert pool.holders() == ["c", "d"]

    asyncio.run(scenario())


def test_a_cancelled_waiter_leaves_the_queue():
    async def scenario() -> None:
        sig = Signal()
        pool = SlotPool("test", 1, sig.poke)
        await pool.acquire("a")
        gone = asyncio.create_task(pool.acquire("b"))
        await sig.until(lambda: pool.waiting() == ["b"])
        gone.cancel()
        await asyncio.gather(gone, return_exceptions=True)
        assert pool.waiting() == []
        pool.release("a")
        assert pool.holders() == []

    asyncio.run(scenario())


# ---- the scheduler: many stories in flight, N model calls ------------------


def test_model_slots_cap_overlapping_calls_with_six_eligible_stories(tmp_path, monkeypatch):
    """6 queued stories, model_slots=2: the scheduler starts all six, at most
    two fake-provider calls overlap (peak counter), the other four wait for a
    slot; the board shows the slot state of each."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "six.db"))
    gw = SlotGateway(block=(SPEC,))
    with TestClient(create_app(gateway=gw)) as client:
        client.patch(
            "/api/v1/settings", json={"story_poll_interval": 0.05, "model_slots": 2}
        )
        runner = client.app.state.story_runner
        ids = [
            client.post("/api/v1/stories", json={"title": f"six {i}"}).json()["id"]
            for i in range(6)
        ]

        async def scenario() -> None:
            sig = Signal()
            gw.sig = sig
            watch(runner, sig)
            q = await bus.subscribe()
            try:
                await asyncio.gather(
                    *(asyncio.to_thread(client.post, f"/api/v1/stories/{sid}/queue") for sid in ids)
                )
                pool = runner._model_pool
                await sig.until(lambda: len(pool.waiting()) == 4 or gw.active > 2)
                assert gw.active == 2 and len(pool.holders()) == 2
                assert set(pool.holders()) | set(pool.waiting()) == set(ids)
                assert all(runner.is_running(sid) for sid in ids)
                states = {sid: runner_mod.slot_state(sid) for sid in ids}
                assert sorted(states.values()) == ["waiting_slot"] * 4 + ["working"] * 2
                gw.release.set()
                parked: set[str] = set()
                slot_events: list[dict] = []
                while parked != set(ids):
                    msg = json.loads(await asyncio.wait_for(q.get(), WAIT))
                    if msg["kind"] == "story_slot":
                        slot_events.append(msg["data"])
                    story = msg["data"].get("story") or {}
                    if msg["kind"] == "story" and story.get("status") == "human_review":
                        parked.add(story["id"])
                assert {e["slot_state"] for e in slot_events} >= {"working", None}
            finally:
                bus.unsubscribe(q)

        client.portal.call(scenario)
        assert gw.calls(SPEC) == 6
        assert gw.peak == 2
        # API carries slot_state; parked stories hold none
        for sid in ids:
            body = client.get(f"/api/v1/stories/{sid}").json()
            assert body["status"] == "human_review"
            assert "slot_state" in body and body["slot_state"] is None


def test_a_story_parked_at_a_human_gate_holds_no_slot(tmp_path):
    gw = SlotGateway()
    runner = make_runner(tmp_path, gw)
    sid = add_story(runner, "parked", StoryStatus.IN_PROGRESS)

    async def scenario() -> None:
        runner._model_pool.resize(1)
        await runner._run_story(sid)  # spec, then AI review approves
        assert status_of(runner, sid) == StoryStatus.HUMAN_REVIEW
        assert runner._model_pool.holders() == [] and runner._test_pool.holders() == []
        assert runner_mod.slot_state(sid) is None
        # the next story gets the single slot straight away
        other = add_story(runner, "next", StoryStatus.IN_PROGRESS)
        await asyncio.wait_for(runner._generate_spec(other), WAIT)
        assert status_of(runner, other) == StoryStatus.AI_REVIEW

    asyncio.run(scenario())
    assert gw.calls(SPEC) == 2


def test_a_429_frees_the_model_slot_for_a_waiting_story(tmp_path):
    gw = SlotGateway()
    runner = make_runner(tmp_path, gw)
    a = add_story(runner, "alpha-429", StoryStatus.IN_PROGRESS)
    b = add_story(runner, "bravo-waits", StoryStatus.IN_PROGRESS)

    async def scenario() -> None:
        sig = Signal()
        gw.sig = sig
        watch(runner, sig)
        gw.gates = {"alpha-429": asyncio.Event(), "bravo-waits": asyncio.Event()}
        gw.fail_429 = {"alpha-429"}
        runner._model_pool.resize(1)
        runner._running.update({a, b})
        ta = asyncio.create_task(runner._run_story(a))
        await sig.until(lambda: runner._model_pool.holders() == [a] and gw.active == 1)
        tb = asyncio.create_task(runner._run_story(b))
        await sig.until(lambda: runner._model_pool.waiting() == [b])
        assert runner_mod.slot_state(b) == "waiting_slot"
        gw.gates["alpha-429"].set()  # alpha's call answers 429
        await asyncio.wait_for(ta, WAIT)
        await sig.until(lambda: runner._model_pool.holders() == [b])
        assert runner_mod.slot_state(a) is None
        assert runner_mod.slot_state(b) == "working"
        assert a in runner._backoff  # the normal rate-limit requeue
        gw.gates["bravo-waits"].set()
        await asyncio.wait_for(tb, WAIT)

    asyncio.run(scenario())
    assert status_of(runner, a) == StoryStatus.QUEUED
    assert status_of(runner, b) == StoryStatus.HUMAN_REVIEW
    assert gw.peak == 1


def test_ai_reviews_hold_a_model_slot_per_call(tmp_path):
    gw = SlotGateway()
    runner = make_runner(tmp_path, gw)
    ids = [
        add_story(runner, f"review {i}", StoryStatus.AI_REVIEW, spec=MINIMAL_SPEC)
        for i in range(2)
    ]

    async def scenario() -> None:
        sig = Signal()
        gw.sig = sig
        gw.review_gate = asyncio.Event()
        watch(runner, sig)
        runner._model_pool.resize(1)
        tasks = [asyncio.create_task(runner._run_ai_review(sid)) for sid in ids]
        await sig.until(
            lambda: gw.active >= 2 or (gw.active == 1 and runner._model_pool.waiting() == [ids[1]])
        )
        assert gw.active == 1 and runner._model_pool.holders() == [ids[0]]
        gw.review_gate.set()
        await asyncio.wait_for(asyncio.gather(*tasks), WAIT)

    asyncio.run(scenario())
    assert gw.calls(AI_REVIEW) == 2 and gw.peak == 1
    assert {status_of(runner, sid) for sid in ids} == {StoryStatus.HUMAN_REVIEW}


# ---- verify: test slots, and no model slot while verifying -----------------


class FakeVerify:
    """Stands in for _run_verify_command (the real _verify_and_fix calls it in
    a worker thread): counts overlapping runs and blocks until released."""

    def __init__(self, sig: Signal) -> None:
        self.sig = sig
        self.gate = threading.Event()
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.started: list[str] = []
        self.model_holders_seen: list[list[str]] = []
        self.runner: StoryRunner | None = None

    def __call__(self, ws, command, timeout, project_root=None, story_id=None, executor=None):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.started.append(story_id)
            if self.runner is not None:
                self.model_holders_seen.append(self.runner._model_pool.holders())
        self.sig.poke_threadsafe()
        try:
            assert self.gate.wait(WAIT)
            return 0, "1 passed in 0.01s\n"
        finally:
            with self.lock:
                self.running -= 1
            self.sig.poke_threadsafe()


async def no_round(prompt: str) -> None:  # pragma: no cover - verify passes
    raise AssertionError("no fix round expected")


def test_a_story_in_verify_holds_no_model_slot_and_another_story_generates(
    tmp_path, monkeypatch
):
    gw = SlotGateway()
    runner = make_runner(tmp_path, gw, verify_command="true")
    a = add_story(runner, "in verify", StoryStatus.EXECUTING)
    b = add_story(runner, "generating", StoryStatus.IN_PROGRESS)

    async def scenario() -> None:
        sig = Signal(asyncio.get_running_loop())
        gw.sig = sig
        watch(runner, sig)
        verify = FakeVerify(sig)
        verify.runner = runner
        monkeypatch.setattr(runner_mod, "_run_verify_command", verify)
        runner._model_pool.resize(1)
        runner._test_pool.resize(1)
        ta = asyncio.create_task(
            runner._verify_and_fix(a, str(tmp_path), tmp_path, no_round, "t", MINIMAL_SPEC)
        )
        await sig.until(lambda: verify.started == [a])
        assert runner._model_pool.holders() == []
        assert runner._test_pool.holders() == [a]
        assert runner_mod.slot_state(a) == "waiting_tests"
        # the single model slot is free: another story's spec call runs now,
        # while the verify is still in flight
        tb = asyncio.create_task(runner._generate_spec(b))
        await sig.until(lambda: gw.calls(SPEC) == 1, timeout=10)
        assert verify.running == 1
        await asyncio.wait_for(tb, WAIT)
        verify.gate.set()
        result = await asyncio.wait_for(ta, WAIT)
        assert result["passed"] is True
        assert verify.model_holders_seen == [[]]

    asyncio.run(scenario())
    assert status_of(runner, b) == StoryStatus.AI_REVIEW


def test_test_slots_one_never_overlaps_two_verify_runs(tmp_path, monkeypatch):
    gw = SlotGateway()
    runner = make_runner(tmp_path, gw, verify_command="true")
    a = add_story(runner, "verify a", StoryStatus.EXECUTING)
    b = add_story(runner, "verify b", StoryStatus.EXECUTING)

    async def scenario() -> FakeVerify:
        sig = Signal(asyncio.get_running_loop())
        watch(runner, sig)
        verify = FakeVerify(sig)
        monkeypatch.setattr(runner_mod, "_run_verify_command", verify)
        runner._test_pool.resize(1)
        runner._model_pool.resize(16)
        tasks = [
            asyncio.create_task(
                runner._verify_and_fix(sid, str(tmp_path), tmp_path, no_round, "t", MINIMAL_SPEC)
            )
            for sid in (a, b)
        ]
        # the second either queues for the test slot or (a broken pool)
        # starts alongside the first
        await sig.until(
            lambda: len(verify.started) >= 2
            or (len(verify.started) == 1 and len(runner._test_pool.waiting()) == 1)
        )
        assert verify.started == [a] and runner._test_pool.waiting() == [b]
        assert runner_mod.slot_state(b) == "waiting_tests"
        verify.gate.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), WAIT)
        assert all(r["passed"] for r in results)
        return verify

    verify = asyncio.run(scenario())
    assert verify.started == [a, b]
    assert verify.peak == 1


class GatedVerify(FakeVerify):
    """FakeVerify with one gate per story; the story in `fail` raises once
    its gate opens (a crashed verify run)."""

    def __init__(self, sig: Signal, fail: str | None = None) -> None:
        super().__init__(sig)
        self.fail = fail
        self.gates: dict[str, threading.Event] = {}

    def __call__(self, ws, command, timeout, project_root=None, story_id=None, executor=None):
        gate = self.gates.setdefault(story_id, threading.Event())
        with self.lock:
            self.started.append(story_id)
        self.sig.poke_threadsafe()
        assert gate.wait(WAIT)
        if story_id == self.fail:
            raise RuntimeError("verify crashed")
        return 0, "1 passed in 0.01s\n"


@pytest.mark.parametrize("ending", ["raises", "cancelled"])
def test_a_verify_that_raises_or_is_cancelled_frees_the_only_test_slot(
    tmp_path, monkeypatch, ending
):
    """A watchdog cancel of a hung verify (or a crashed one) must not leak
    the only test slot: the next story's queued verify starts at once."""
    gw = SlotGateway()
    runner = make_runner(tmp_path, gw, verify_command="true")
    a = add_story(runner, "verify ends badly", StoryStatus.EXECUTING)
    b = add_story(runner, "verify queued", StoryStatus.EXECUTING)

    async def scenario() -> None:
        sig = Signal(asyncio.get_running_loop())
        watch(runner, sig)
        verify = GatedVerify(sig, fail=a if ending == "raises" else None)
        verify.gates = {a: threading.Event(), b: threading.Event()}
        monkeypatch.setattr(runner_mod, "_run_verify_command", verify)
        runner._test_pool.resize(1)
        runner._model_pool.resize(16)

        def start(sid: str) -> asyncio.Task:
            return asyncio.create_task(
                runner._verify_and_fix(sid, str(tmp_path), tmp_path, no_round, "t", MINIMAL_SPEC)
            )

        ta = start(a)
        await sig.until(lambda: verify.started == [a])
        tb = start(b)
        await sig.until(lambda: runner._test_pool.waiting() == [b])
        assert runner._test_pool.holders() == [a]
        if ending == "raises":
            verify.gates[a].set()
            with pytest.raises(RuntimeError, match="verify crashed"):
                await asyncio.wait_for(ta, WAIT)
        else:
            ta.cancel()
            with pytest.raises(asyncio.CancelledError):
                await ta
        assert a not in runner._test_pool.holders()
        await sig.until(lambda: verify.started == [a, b])
        assert runner._test_pool.holders() == [b]
        verify.gates[b].set()
        verify.gates[a].set()  # let the cancelled run's thread finish
        result = await asyncio.wait_for(tb, WAIT)
        assert result["passed"] is True
        assert runner._test_pool.holders() == []
        assert runner._test_pool.waiting() == []

    asyncio.run(scenario())


# ---- coder rounds: pump and headless ---------------------------------------


def test_pump_coder_rounds_share_the_model_slots(tmp_path, legacy_review):
    gw = SlotGateway(block=(CODE,))
    runner = make_runner(tmp_path, gw, **coder_settings(tmp_path))
    ids = [
        add_story(runner, f"pump {i}", StoryStatus.EXECUTING, spec=MINIMAL_SPEC)
        for i in range(2)
    ]

    async def scenario() -> None:
        sig = Signal()
        gw.sig = sig
        watch(runner, sig)
        runner._model_pool.resize(1)
        launch_coders(runner, ids)
        await sig.until(
            lambda: gw.active >= 2
            or (gw.calls(CODE) == 1 and len(runner._model_pool.waiting()) == 1)
        )
        # which one asks first depends on its worktree setup; one holds the
        # slot, the other waits for it
        pool = runner._model_pool
        assert gw.active == 1 and sorted(pool.holders() + pool.waiting()) == sorted(ids)
        gw.review_gate = asyncio.Event()
        gw.release.set()
        # the code reviews (the reviewer's stream) share the slot as well
        await sig.until(
            lambda: gw.active >= 2
            or (gw.calls(CODE_REVIEW) == 1 and len(pool.waiting()) == 1)
        )
        assert gw.active == 1
        gw.review_gate.set()
        await drain(runner)

    asyncio.run(scenario())
    assert gw.calls(CODE_REVIEW) == 2
    assert gw.calls(CODE) == 4  # per story: the write round, then "done"
    assert gw.peak == 1
    assert {status_of(runner, sid) for sid in ids} == {StoryStatus.HUMAN_REVIEW}


def test_headless_runs_hold_a_model_slot_for_the_whole_cli_run(tmp_path, monkeypatch):
    gw = SlotGateway()
    runner = make_runner(tmp_path, gw, **coder_settings(tmp_path))
    ids = [
        add_story(
            runner, f"headless {i}", StoryStatus.EXECUTING, spec=MINIMAL_SPEC,
            execution="headless",
        )
        for i in range(2)
    ]

    async def scenario() -> None:
        sig = Signal(asyncio.get_running_loop())
        gw.sig = sig
        watch(runner, sig)
        gate = threading.Event()
        lock = threading.Lock()
        state = {"active": 0, "peak": 0, "runs": 0}

        def fake_code_stage(*, story_id, worktree, prompt, config, on_event):
            with lock:
                state["active"] += 1
                state["runs"] += 1
                state["peak"] = max(state["peak"], state["active"])
            sig.poke_threadsafe()
            try:
                assert gate.wait(WAIT)
                (Path(worktree) / f"{story_id}.txt").write_text("x\n")
                return headless_exec.HeadlessOutcome(outcome="ok", num_turns=1, duration_ms=1)
            finally:
                with lock:
                    state["active"] -= 1
                sig.poke_threadsafe()

        monkeypatch.setattr(headless_exec, "run_code_stage", fake_code_stage)
        runner._model_pool.resize(1)
        launch_coders(runner, ids)
        await sig.until(
            lambda: state["active"] >= 2
            or (state["active"] == 1 and len(runner._model_pool.waiting()) == 1)
        )
        pool = runner._model_pool
        assert state["active"] == 1 and sorted(pool.holders() + pool.waiting()) == sorted(ids)
        assert runner_mod.slot_state(pool.holders()[0]) == "working"
        assert runner_mod.slot_state(pool.waiting()[0]) == "waiting_slot"
        gate.set()
        await drain(runner)
        assert state["runs"] == 2 and state["peak"] == 1

    asyncio.run(scenario())
    assert gw.peak == 1  # the code reviews take the slot one at a time too


def test_headless_suite_runs_outside_the_model_slot_and_resumes_the_session(
    tmp_path, monkeypatch
):
    from test_headless import install_fake_claude, on_path

    capture = tmp_path / "capture.txt"
    bindir = install_fake_claude(
        tmp_path, "runtests", env={"FAKE_CLAUDE_CAPTURE": str(capture)}
    )
    gw = SlotGateway()
    runner = make_runner(
        tmp_path, gw, **coder_settings(tmp_path, verify_command="true")
    )
    a = add_story(
        runner, "headless", StoryStatus.EXECUTING, spec=MINIMAL_SPEC, execution="headless"
    )
    b = add_story(runner, "generating", StoryStatus.IN_PROGRESS)

    async def scenario() -> None:
        sig = Signal(asyncio.get_running_loop())
        gw.sig = sig
        watch(runner, sig)
        verify = FakeVerify(sig)
        verify.runner = runner
        monkeypatch.setattr(runner_mod, "_run_verify_command", verify)
        runner._model_pool.resize(1)
        runner._test_pool.resize(1)
        with on_path(bindir):
            launch_coders(runner, [a])
            await sig.until(lambda: verify.started == [a])
            # the suite runs outside the session: no model slot, one test slot
            assert runner._model_pool.holders() == []
            assert runner._test_pool.holders() == [a]
            # the single model slot is free: another story's spec call runs
            tb = asyncio.create_task(runner._generate_spec(b))
            await sig.until(lambda: gw.calls(SPEC) == 1, timeout=10)
            assert verify.running == 1
            await asyncio.wait_for(tb, WAIT)
            verify.gate.set()
            await drain(runner)

    asyncio.run(scenario())
    calls = capture.read_text().split("--- end call ---")
    assert len([c for c in calls if c.strip()]) == 1
    resumed = calls[0].splitlines()
    assert resumed[resumed.index("--resume") + 1] == "sess-fake-1"
    assert "=== TEST RESULT ===" in calls[0]
    assert "Exit code: 0" in calls[0]
    assert "1 passed" in calls[0]
    assert status_of(runner, a) != StoryStatus.EXECUTING


def test_a_fourth_suite_request_ends_the_round_as_ok_and_cuts_are_logged(
    tmp_path, monkeypatch
):
    gw = SlotGateway()
    runner = make_runner(
        tmp_path, gw, **coder_settings(tmp_path, verify_command="true")
    )
    a = add_story(
        runner, "headless", StoryStatus.EXECUTING, spec=MINIMAL_SPEC, execution="headless"
    )
    logs: list[str] = []
    real_publish = bus.publish

    def record(kind, payload, *a, **kw):
        if kind == "story_log":
            logs.append(payload["text"])
        return real_publish(kind, payload, *a, **kw)

    monkeypatch.setattr(bus, "publish", record)
    segments: list[dict] = []

    def fake_code_stage(*, story_id, worktree, prompt, config, on_event, **resume):
        segments.append(resume)
        (Path(worktree) / "feature.py").write_text("x = 1\n")
        return headless_exec.HeadlessOutcome(
            outcome="run_tests", num_turns=1, duration_ms=1, session_id="sid",
            config_dir=str(tmp_path / "kept"), targeted_cuts=1,
        )

    suites: list[str] = []

    def verify(ws, command, timeout, project_root=None, story_id=None, executor=None):
        suites.append(story_id)
        return 0, "1 passed in 0.01s\n"

    monkeypatch.setattr(headless_exec, "run_code_stage", fake_code_stage)
    monkeypatch.setattr(runner_mod, "_run_verify_command", verify)
    (tmp_path / "kept").mkdir()

    async def scenario() -> None:
        launch_coders(runner, [a])
        await drain(runner)

    asyncio.run(scenario())
    # 1 first segment + 3 resumed ones; the 4th request is a finish
    assert len(segments) == 4 and segments[0] == {}
    assert all(seg["resume_session_id"] == "sid" for seg in segments[1:])
    assert any("test request cap (3) reached" in t for t in logs)
    assert sum("targeted test run cut at" in t for t in logs) == 4
    assert not (tmp_path / "kept").exists()
    assert status_of(runner, a) != StoryStatus.EXECUTING


# ---- worker pods only inside a slot ----------------------------------------


@pytest.fixture
def in_cluster(monkeypatch):
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


def test_coder_pods_are_created_only_inside_a_model_slot(tmp_path, in_cluster):
    """6 executing stories, model_slots=2, run_executor=pod: all six are in
    flight, but only the two holding a slot have a worker pod."""
    from test_pod_executor import FakeKube

    kube = FakeKube()
    pods.set_kube_factory(lambda: kube)
    gw = SlotGateway(block=(CODE,))
    runner = make_runner(
        tmp_path, gw, run_executor="pod", worker_image="20261102-1",
        **coder_settings(tmp_path),
    )
    ids = [
        add_story(runner, f"pod {i}", StoryStatus.EXECUTING, spec=MINIMAL_SPEC)
        for i in range(6)
    ]

    async def scenario() -> None:
        sig = Signal()
        gw.sig = sig
        watch(runner, sig)
        runner._model_pool.resize(2)
        launch_coders(runner, ids)
        assert len(runner._executors) == 6  # executor objects only, no pods
        await sig.until(
            lambda: (gw.active == 2 and len(runner._model_pool.waiting()) == 4)
            or len(kube.created) > 2
        )
        assert len(kube.created) == 2
        assert sorted(p["metadata"]["labels"][pods.story_label()] for p in kube.created) == sorted(
            runner._model_pool.holders()
        )
        gw.release.set()
        await drain(runner)

    asyncio.run(scenario())
    assert len(kube.created) == 6  # every run got its pod, each inside its slot
    assert gw.peak == 2


def test_spec_pod_sessions_build_their_pod_only_inside_a_model_slot(
    tmp_path, monkeypatch, in_cluster
):
    from test_claude_code_phases import MODEL, TOKEN
    from test_headless import _init_repo
    from test_pod_spec import SpecKube, cli_json, pod_client

    class BlockingSpecKube(SpecKube):
        def __init__(self) -> None:
            super().__init__([cli_json()])
            self.gate = threading.Event()
            self.lock = threading.Lock()
            self.active = 0
            self.peak = 0
            self.sig: Signal | None = None

        def exec(self, name, argv, stdin_bytes, on_output, timeout):
            if not (argv[:2] == ["sh", "-c"] and argv[2] == pods.HEADLESS_SCRIPT):
                return super().exec(name, argv, stdin_bytes, on_output, timeout)
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            self.sig.poke_threadsafe()
            try:
                assert self.gate.wait(WAIT)
                return super().exec(name, argv, stdin_bytes, on_output, timeout)
            finally:
                with self.lock:
                    self.active -= 1
                self.sig.poke_threadsafe()

    kube = BlockingSpecKube()
    from conftest import FakeGateway

    client = pod_client(tmp_path, monkeypatch, FakeGateway(), kube, model_slots=2)
    try:
        runner = client.app.state.story_runner
        client.post("/api/v1/providers/claude-max/key", json={"key": TOKEN})
        proj = client.post(
            "/api/v1/projects", json={"name": "slots", "root": _init_repo(tmp_path / "proj")}
        ).json()
        ids = [
            client.post(
                "/api/v1/stories",
                json={"title": f"spec {i}", "project_id": proj["id"], "model": MODEL},
            ).json()["id"]
            for i in range(6)
        ]

        async def scenario() -> None:
            sig = Signal(asyncio.get_running_loop())
            kube.sig = sig
            watch(runner, sig)
            await asyncio.gather(
                *(asyncio.to_thread(client.post, f"/api/v1/stories/{sid}/queue") for sid in ids)
            )
            pool = runner._model_pool
            await sig.until(
                lambda: (kube.active == 2 and len(pool.waiting()) == 4) or len(kube.created) > 2
            )
            assert len(kube.created) == 2
            kube.gate.set()
            await sig.until(lambda: len(kube.frames) == 6 and kube.active == 0)

        client.portal.call(scenario)
        assert kube.peak == 2
        assert len(kube.frames) == 6
    finally:
        client.__exit__(None, None, None)


def test_one_tick_starts_every_orphan_and_they_queue_for_the_slot(tmp_path):
    """No max_parallel early return: one tick starts a task for every
    eligible story; with model_slots=1 one generates, the rest wait. (Grant
    order is the order of acquire() calls; each run's thread-side prep before
    its first call decides that, see the pool tests for the FIFO rule.)"""
    gw = SlotGateway(block=(SPEC,))
    runner = make_runner(tmp_path, gw)
    ids = [add_story(runner, f"orphan {i}", StoryStatus.IN_PROGRESS) for i in range(3)]

    async def scenario() -> None:
        sig = Signal()
        gw.sig = sig
        watch(runner, sig)
        runner._next_proc_sweep = float("inf")
        await runner._tick(1)
        assert set(runner._tasks) == set(ids)
        await sig.until(lambda: len(runner._model_pool.waiting()) == 2)
        pool = runner._model_pool
        assert gw.active == 1 and sorted(pool.holders() + pool.waiting()) == sorted(ids)
        gw.release.set()
        await drain(runner)

    asyncio.run(scenario())
    with Session(runner.engine) as db:
        rows = db.exec(select(Story)).all()
    assert {r.status for r in rows} == {StoryStatus.HUMAN_REVIEW}


def test_one_tick_gives_the_first_model_slot_to_an_urgent_story(tmp_path, monkeypatch):
    """With model_slots=1, one _tick hands the first model slot to the
    newest queued story when it is urgent, ahead of older normal ones; the
    normals queue behind it in fair order (oldest first)."""
    gw = SlotGateway(block=(SPEC,))
    runner = make_runner(tmp_path, gw)
    now = runner_mod.utcnow()
    oldest = add_story(
        runner, "normal oldest", StoryStatus.QUEUED, queued_at=now - timedelta(seconds=300)
    )
    older = add_story(
        runner, "normal older", StoryStatus.QUEUED, queued_at=now - timedelta(seconds=200)
    )
    urgent = add_story(
        runner,
        "urgent newest",
        StoryStatus.QUEUED,
        priority="urgent",
        queued_at=now - timedelta(seconds=10),
    )

    async def no_sync(*args) -> None:
        # the push-root sync hops to a thread before the spec writer asks
        # for its slot; without it a task asks in launch (= tick) order
        return None

    monkeypatch.setattr(runner, "_sync_push_root_async", no_sync)

    async def scenario() -> None:
        sig = Signal()
        gw.sig = sig
        watch(runner, sig)
        runner._next_proc_sweep = float("inf")
        await runner._tick(1)
        await sig.until(lambda: len(runner._model_pool.waiting()) == 2)
        assert runner._model_pool.holders() == [urgent]
        assert runner._model_pool.waiting() == [oldest, older]
        assert gw.active == 1
        gw.release.set()
        await drain(runner)

    asyncio.run(scenario())
