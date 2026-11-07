"""Worker pods step 5: the cap on coder runs on the backend's own node."""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlmodel import select
from test_pod_executor import FakeKube
from test_proc_groups import _make_runner

from app import pods, settings_store
from app.db import session_scope
from app.models import Story, StoryEvent, StoryNote, StoryStatus


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("POD_NAME", "fubala-0")
    monkeypatch.setenv("POD_UID", "uid-123")
    monkeypatch.setenv("NODE_NAME", "stor01")
    monkeypatch.setenv("FUBALA_WORKER_IMAGE_REPO", "registry.example.com/fubala/fubala-worker")
    monkeypatch.setattr(pods, "preflight", lambda image: None)
    pods._FALLBACK_HELD.clear()
    pods._WARNED.clear()
    yield
    pods._FALLBACK_HELD.clear()
    pods._LIVE.clear()
    pods.set_kube_factory(None)


def _setup(tmp_path, monkeypatch, kube, mode="verify"):
    r = _make_runner(tmp_path)
    settings_store.save_settings(r.engine, {"run_executor": mode, "worker_image": "t1"})
    monkeypatch.setattr(pods, "_kube_factory", lambda: kube)
    with session_scope(r.engine) as db:
        db.add(Story(id="s3", title="t", status=StoryStatus.IN_PROGRESS))
    return r


def _state(r):
    with session_scope(r.engine) as db:
        story = db.get(Story, "s3")
        notes = db.exec(select(StoryNote).where(StoryNote.story_id == "s3")).all()
        events = [
            json.loads(e.data)
            for e in db.exec(select(StoryEvent).where(StoryEvent.story_id == "s3")).all()
        ]
        return story.status, [n.body for n in notes], events


def _gate(r, with_executor=True):
    async def scenario():
        if with_executor:
            ex = r._make_executor("s3", asyncio.get_running_loop())
            r._executors["s3"] = ex
        return await r._fallback_gate("s3")

    return asyncio.run(scenario())


def test_third_run_on_backend_node_is_requeued(tmp_path, monkeypatch):
    kube = FakeKube(node="stor01")
    r = _setup(tmp_path, monkeypatch, kube)
    pods._FALLBACK_HELD.update({"s1", "s2"})
    assert _gate(r) is False
    status, notes, events = _state(r)
    assert status == StoryStatus.QUEUED
    assert notes == ["waiting: dev node full or absent, fallback cap 2"]
    assert events[-1]["fallback_cap_wait"] is True
    assert "s3" in r._backoff
    assert len(kube.deleted) == 1
    assert "s3" not in pods._FALLBACK_HELD


def test_repeated_wait_adds_one_note(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, FakeKube(node="stor01"))
    pods._FALLBACK_HELD.update({"s1", "s2"})
    _gate(r)
    with session_scope(r.engine) as db:
        db.get(Story, "s3").status = StoryStatus.IN_PROGRESS
        db.add(StoryEvent(story_id="s3", kind="status", data="{}"))
    _gate(r)
    assert len(_state(r)[1]) == 1


def test_dev_node_starts_and_claims_nothing(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, FakeKube(node="dev1"))
    pods._FALLBACK_HELD.update({"s1", "s2"})
    assert _gate(r) is True
    assert pods._FALLBACK_HELD == {"s1", "s2"}
    assert _state(r)[0] == StoryStatus.IN_PROGRESS


def test_restart_counts_listed_pods():
    kube = FakeKube(
        listed=[
            {"name": "a", "story": "s1", "node": "stor01", "phase": "Running"},
            {"name": "b", "story": "s2", "node": "stor01", "phase": "Pending"},
            {"name": "c", "story": "s4", "node": "stor01", "phase": "Succeeded"},
            {"name": "d", "story": "s5", "node": "dev1", "phase": "Running"},
        ]
    )
    assert pods.try_claim_fallback("s3", 2, kube) is False
    assert pods.try_claim_fallback("s1", 2, kube) is True  # own id not counted


def test_pod_and_held_claim_of_same_story_count_once():
    kube = FakeKube(listed=[{"name": "a", "story": "s1", "node": "stor01", "phase": "Running"}])
    pods._FALLBACK_HELD.add("s1")
    assert pods.try_claim_fallback("s3", 2, kube) is True


def test_listing_failure_counts_held_only():
    class Broken(FakeKube):
        def list_pods(self, label_selector):
            raise RuntimeError("api down")

    assert pods.try_claim_fallback("s3", 2, Broken()) is True


def test_in_process_runs_count(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, FakeKube(), mode="in_process")
    pods._FALLBACK_HELD.update({"s1", "s2"})
    assert _gate(r) is False
    assert _state(r)[0] == StoryStatus.QUEUED


def test_no_node_name_keeps_old_behaviour(tmp_path, monkeypatch):
    monkeypatch.delenv("NODE_NAME")
    r = _setup(tmp_path, monkeypatch, FakeKube(), mode="in_process")
    pods._FALLBACK_HELD.update({"s1", "s2"})
    assert _gate(r) is True


def _cap0(r, mode="pod"):
    settings_store.save_settings(r.engine, {"run_executor": mode, "worker_fallback_parallel": 0})


def _no_local(monkeypatch):
    from app.providers import claude_code as cc
    from app import procs

    def boom(*a, **k):
        raise AssertionError("ran locally with fallback cap 0")

    monkeypatch.setattr(procs, "LocalExecutor", boom)
    monkeypatch.setattr(cc, "run_cli_agent", boom)


def test_defaults_and_helpers(monkeypatch):
    assert settings_store.DEFAULTS["worker_fallback_parallel"] == 2
    assert pods.fallback_cap({}) == 2
    assert pods.fallback_cap({"worker_fallback_parallel": None}) == 2
    assert pods.fallback_cap({"worker_fallback_parallel": 0}) == 0
    assert pods.local_fallback_disabled({"worker_fallback_parallel": 0}) is True
    assert pods.local_fallback_disabled({"worker_fallback_parallel": 1}) is False
    monkeypatch.delenv("NODE_NAME")
    assert pods.local_fallback_disabled({"worker_fallback_parallel": 0}) is False


def test_cap0_pending_dev_node_requeues_without_local_run(tmp_path, monkeypatch):
    _no_local(monkeypatch)
    monkeypatch.setattr(pods, "PENDING_LIMIT", 0)
    r = _setup(tmp_path, monkeypatch, FakeKube(phases=("Pending",)), mode="pod")
    _cap0(r)
    assert _gate(r) is False
    status, notes, _ = _state(r)
    assert status == StoryStatus.QUEUED
    assert "fallback cap 0" in notes[0]


def test_cap0_pod_on_backend_node_is_requeued_and_deleted(tmp_path, monkeypatch):
    kube = FakeKube(node="stor01")
    r = _setup(tmp_path, monkeypatch, kube, mode="pod")
    _cap0(r)
    assert not pods._FALLBACK_HELD
    assert _gate(r) is False
    assert _state(r)[0] == StoryStatus.QUEUED
    assert len(kube.deleted) == 1


def test_cap0_without_node_name_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("NODE_NAME")
    r = _setup(tmp_path, monkeypatch, FakeKube(), mode="in_process")
    settings_store.save_settings(r.engine, {"worker_fallback_parallel": 0})
    assert _gate(r, with_executor=False) is True


def test_cap0_verify_command_never_runs_locally(tmp_path, monkeypatch):
    from app import runner as runner_mod

    _no_local(monkeypatch)
    monkeypatch.setattr(pods, "PENDING_LIMIT", 0)
    r = _setup(tmp_path, monkeypatch, FakeKube(phases=("Pending",)), mode="pod")
    _cap0(r)

    async def scenario():
        ex = r._make_executor("s3", asyncio.get_running_loop())
        with pytest.raises(pods.FallbackCapFull):
            await asyncio.to_thread(
                runner_mod._run_verify_command, tmp_path, "true", 5, None, "s3", ex
            )

    asyncio.run(scenario())
