"""Verify-only PodExecutor (worker pods 1c), driven by a fake KubeApi."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path

import pytest

from app import pods, procs, settings_store
from app.agent_tools import ToolBox
from app.runner import _run_verify_command

SETTINGS = {
    "run_executor": "verify",
    "worker_image": "20261102-1",
    "headless_timeout_seconds": 2400.0,
    "verify_timeout": 600.0,
    "verify_fix_rounds": 2,
}


@pytest.fixture(autouse=True)
def worker_image_repo(monkeypatch):
    monkeypatch.setenv("FUBALA_WORKER_IMAGE_REPO", "registry.example.com/fubala/fubala-worker")


class FakeKube:
    namespace = "fubala"

    def __init__(self, *, create_error=None, phases=("Running",), node="dev1", listed=()):
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.execs: list[tuple[list[str], bytes | None]] = []
        self.create_error = create_error
        self.phases = list(phases)
        self.node = node
        self.listed = list(listed)
        self.exec_result = (0, "ok\n", "")
        self.exec_error: Exception | None = None

    def create_pod(self, body):
        if self.create_error:
            raise self.create_error
        self.created.append(body)

    def read_pod(self, name):
        phase = self.phases.pop(0) if len(self.phases) > 1 else self.phases[0]
        return {"phase": phase, "node": self.node}

    def delete_pod(self, name):
        self.deleted.append(name)

    def list_pods(self, label_selector):
        assert label_selector == pods.MANAGED_SELECTOR
        return [{"node": None, "phase": "Running", **p} for p in self.listed]

    def exec(self, name, argv, stdin_bytes, on_output, timeout):
        if self.exec_error:
            raise self.exec_error
        self.execs.append((argv, stdin_bytes))
        if on_output:
            on_output(self.exec_result[1])
        return self.exec_result


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, secs):
        self.now += secs


@pytest.fixture(autouse=True)
def pod_env(monkeypatch):
    monkeypatch.setenv("POD_NAME", "fubala-0")
    monkeypatch.setenv("POD_UID", "uid-123")
    monkeypatch.setenv("NODE_NAME", "stor01")
    pods._WARNED.clear()
    yield
    pods.set_kube_factory(None)
    pods._LIVE.clear()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"],
        cwd=repo, check=True,
    )  # fmt: skip
    return repo


def executor(kube, lines=None, clock=None, **settings):
    clock = clock or Clock()
    return pods.PodExecutor(
        "abc123",
        {**SETTINGS, **settings},
        story_log=(lines.append if lines is not None else None),
        kube_factory=lambda: kube,
        clock=clock,
        sleep=clock.sleep,
    )


# ---- pod spec ---------------------------------------------------------------------


def spec(**kw):
    args = dict(
        name="p", story_id="abc123", image="img:1", owner_name="fubala-0",
        owner_uid="uid-123", node_name="stor01", deadline_seconds=100,
    )  # fmt: skip
    return pods.build_pod_spec(**{**args, **kw})


# Frozen from build_pod_spec before the homelab values became settings.
FROZEN_HOUSEHOLD_SPEC = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {
        "name": "p",
        "labels": {"app.kubernetes.io/managed-by": "fubala", "fubala.hoeve.nu/story": "abc123"},
        "ownerReferences": [
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "name": "fubala-0",
                "uid": "uid-123",
                "blockOwnerDeletion": False,
                "controller": False,
            }
        ],
    },
    "spec": {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "activeDeadlineSeconds": 100,
        "tolerations": [
            {"key": "hoeve.nu/dev", "operator": "Equal", "value": "true", "effect": "NoSchedule"}
        ],
        "affinity": {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {"key": "hoeve.nu/dev", "operator": "In", "values": ["true"]}
                            ]
                        },
                        {
                            "matchExpressions": [
                                {
                                    "key": "kubernetes.io/hostname",
                                    "operator": "In",
                                    "values": ["stor01"],
                                }
                            ]
                        },
                    ]
                },
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 100,
                        "preference": {
                            "matchExpressions": [
                                {"key": "hoeve.nu/dev", "operator": "In", "values": ["true"]}
                            ]
                        },
                    }
                ],
            }
        },
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10003,
            "runAsGroup": 10003,
            "fsGroup": 10003,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "worker",
                "image": "img:1",
                "command": [
                    "sh",
                    "-c",
                    'hb="/work/.heartbeat"; [ -e "$hb" ] || touch '
                    '"$hb"; while :; do now=$(date +%s); last=$(date '
                    '-r "$hb" +%s 2>/dev/null || echo 0); [ $((now - '
                    "last)) -gt 900 ] && exit 0; sleep 30; done",
                ],
                "env": [
                    {"name": "HOME", "value": "/cache/home"},
                    {"name": "npm_config_cache", "value": "/cache/npm"},
                    {"name": "UV_CACHE_DIR", "value": "/cache/uv"},
                    {"name": "UV_PYTHON_PREFERENCE", "value": "only-managed"},
                    {"name": "PYTEST_XDIST_AUTO_NUM_WORKERS", "value": "4"},
                ],
                "resources": {
                    "requests": {"cpu": "1500m", "memory": "2Gi"},
                    "limits": {"cpu": "4", "memory": "6Gi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "runAsNonRoot": True,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "volumeMounts": [
                    {"name": "work", "mountPath": "/work"},
                    {"name": "cache", "mountPath": "/cache"},
                ],
            }
        ],
        "volumes": [{"name": "work", "emptyDir": {}}, {"name": "cache", "emptyDir": {}}],
    },
}


def test_household_env_reproduces_the_frozen_spec():
    # the household values come from the homelab2 deployment env
    body = spec(story_label="fubala.hoeve.nu/story", dev_node_label="hoeve.nu/dev")
    assert body == FROZEN_HOUSEHOLD_SPEC
    assert json.dumps(body) == json.dumps(FROZEN_HOUSEHOLD_SPEC)


def test_neutral_defaults_carry_no_household_values(monkeypatch):
    for var in ("FUBALA_WORKER_IMAGE_REPO", "FUBALA_STORY_LABEL", "FUBALA_DEV_NODE_LABEL"):
        monkeypatch.delenv(var, raising=False)
    body = spec(story_label=pods.story_label(), dev_node_label=pods.dev_node_label())
    assert "hoeve" not in json.dumps(body)
    assert "tolerations" not in body["spec"] and "affinity" not in body["spec"]
    assert list(body["metadata"]["labels"]) == [pods.MANAGED_LABEL, "fubala.dev/story"]
    for v in ("POD_NAME", "POD_UID", "NODE_NAME"):
        monkeypatch.setenv(v, "x")
    assert pods._injected_preflight(SETTINGS) == "FUBALA_WORKER_IMAGE_REPO is not set"
    monkeypatch.setenv("FUBALA_WORKER_IMAGE_REPO", "registry.example.com/w")
    assert pods._injected_preflight(SETTINGS) is None


def test_owner_reference_matches_backend_pod():
    ref = spec()["metadata"]["ownerReferences"][0]
    assert ref == {
        "apiVersion": "v1", "kind": "Pod", "name": "fubala-0", "uid": "uid-123",
        "blockOwnerDeletion": False, "controller": False,
    }  # fmt: skip


def test_pod_spec_is_restricted_and_carries_nothing_from_the_backend(monkeypatch):
    monkeypatch.setenv("SECRET_MODEL_KEY", "sk-leak")
    body = spec(dev_node_label="example.com/dev")
    text = repr(body)
    assert "sk-leak" not in text
    s = body["spec"]
    assert "nodeName" not in s and "kubernetes.io/arch" not in text
    assert "envFrom" not in text and "secret" not in text.lower()
    assert s["automountServiceAccountToken"] is False
    assert s["restartPolicy"] == "Never"
    assert s["securityContext"] == {
        "runAsNonRoot": True, "runAsUser": 10003, "runAsGroup": 10003,
        "fsGroup": 10003, "seccompProfile": {"type": "RuntimeDefault"},
    }  # fmt: skip
    c = s["containers"][0]
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert c["resources"] == {
        "requests": {"cpu": "1500m", "memory": "2Gi"}, "limits": {"cpu": "4", "memory": "6Gi"},
    }  # fmt: skip
    assert {e["name"] for e in c["env"]} == set(pods.POD_ENV)
    assert "UV_PYTHON_INSTALL_DIR" not in pods.POD_ENV
    terms = s["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert len(terms["nodeSelectorTerms"]) == 2
    pref = s["affinity"]["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
    assert pref[0]["weight"] == 100
    assert s["tolerations"][0]["key"] == "example.com/dev"
    labels = body["metadata"]["labels"]
    assert labels[pods.MANAGED_LABEL] == "fubala" and labels["fubala.dev/story"] == "abc123"


def test_pod_spec_resources_come_from_the_arguments():
    c = spec(cpu_request="500m", cpu_limit="2", memory_request="512Mi", memory_limit="3Gi")["spec"][
        "containers"
    ][0]
    assert c["resources"] == {
        "requests": {"cpu": "500m", "memory": "512Mi"},
        "limits": {"cpu": "2", "memory": "3Gi"},
    }


def test_quantity_parsers():
    assert pods.parse_cpu_millicores("1500m") == 1500
    assert pods.parse_cpu_millicores("1.5") == 1500
    assert pods.parse_cpu_millicores("4") == 4000
    assert pods.parse_memory_bytes("256Mi") == 256 << 20
    assert pods.parse_memory_bytes("2Gi") == 2 << 30
    for bad in ("", "1.5m", "x", "-1"):
        with pytest.raises(ValueError):
            pods.parse_cpu_millicores(bad)
    for bad in ("", "2G", "1.5Gi", "512"):
        with pytest.raises(ValueError):
            pods.parse_memory_bytes(bad)


def test_executor_snapshots_resources_at_creation():
    first_settings = {**SETTINGS, "worker_cpu_request": "1", "worker_memory_request": "1Gi"}
    kube = FakeKube()
    first = executor(kube, **first_settings)
    first.prepare()
    # settings saved between two runs: only the next executor sees them
    second_settings = {**first_settings, "worker_cpu_request": "2", "worker_cpu_limit": "8"}
    second = executor(kube, **second_settings)
    second.prepare()
    first.pod_name = None  # the first executor re-creates its pod
    first.node_name = None
    first.prepare()

    def res(body):
        return body["spec"]["containers"][0]["resources"]

    assert len(kube.created) == 3
    assert res(kube.created[0])["requests"] == {"cpu": "1", "memory": "1Gi"}
    assert res(kube.created[1])["requests"] == {"cpu": "2", "memory": "1Gi"}
    assert res(kube.created[1])["limits"]["cpu"] == "8"
    assert res(kube.created[2]) == res(kube.created[0])


def test_deadline_covers_every_verify_round():
    assert pods.pod_deadline(SETTINGS) == 2400 + 600 * 3


def test_watchdog_exits_on_stale_heartbeat_and_survives_a_fresh_one(tmp_path):
    hb = tmp_path / "hb"
    hb.write_text("")
    old = time.time() - 1000
    os.utime(hb, (old, old))
    stale = subprocess.Popen(["sh", "-c", pods.watchdog_script(str(hb), 900, 1)])
    assert stale.wait(timeout=10) == 0

    hb.write_text("")
    os.utime(hb, None)
    fresh = subprocess.Popen(["sh", "-c", pods.watchdog_script(str(hb), 900, 1)])
    try:
        time.sleep(2.5)
        assert fresh.poll() is None
    finally:
        fresh.kill()
        fresh.wait()


def test_add_maxprocesses_only_on_xdist_pytest_segments():
    assert pods.add_maxprocesses("uv run pytest -q -n auto && npm test") == (
        "uv run pytest -q -n auto --maxprocesses=4 && npm test"
    )
    assert pods.add_maxprocesses("pytest -q") == "pytest -q"
    assert pods.add_maxprocesses("pytest -n 2 --maxprocesses=2") == ("pytest -n 2 --maxprocesses=2")


# ---- executor behaviour -------------------------------------------------------------


def test_pending_pod_is_deleted_at_120s_and_verify_runs_in_process(tmp_path):
    repo = make_repo(tmp_path)
    kube = FakeKube(phases=("Pending",))
    lines: list[str] = []
    ex = executor(kube, lines)
    rc, out = _run_verify_command(repo, "echo local", 10, str(repo), "abc123", ex)
    assert rc == 0 and "local" in out
    assert len(kube.deleted) == 1
    assert ex.disabled and not kube.execs
    assert any("running in-process" in line for line in lines)
    # the rest of the run stays local without touching the API again
    created = len(kube.created)
    rc, _ = _run_verify_command(repo, "true", 10, str(repo), "abc123", ex)
    assert rc == 0 and len(kube.created) == created


def test_create_403_falls_back_with_one_warning_per_process(tmp_path, caplog):
    repo = make_repo(tmp_path)
    caplog.set_level(logging.WARNING, logger="app.pods")
    for _ in range(2):
        ex = executor(FakeKube(create_error=RuntimeError("403 Forbidden")))
        rc, out = _run_verify_command(repo, "echo hi", 10, str(repo), "abc123", ex)
        assert rc == 0 and "hi" in out
    assert len([r for r in caplog.records if "fallback (create)" in r.message]) == 1


def test_pod_run_syncs_then_runs_and_maps_cwd(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "sub").mkdir()
    (repo / "sub" / "g.txt").write_text("y\n")
    kube = FakeKube(node="stor01")
    lines: list[str] = []
    ex = executor(kube, lines)
    rc, out = _run_verify_command(repo / "sub", "pytest -n auto", 60, str(repo), "abc123", ex)
    assert (rc, out) == (0, "ok\n")
    sync, run, reap = kube.execs
    assert reap[0] == ["sh", "-c", pods.REAP_SCRIPT]
    assert sync[1].split(b"\n", 1)[0] == str(len(sync[1].split(b"\n", 1)[1])).encode()
    argv = run[0]
    assert argv[4] == "/work/repo/sub" and argv[5] == "pytest -n auto --maxprocesses=4"
    assert run[1] is None
    assert any("on node stor01 (backend node)" in line for line in lines)
    ex.close()
    assert kube.deleted == [kube.created[0]["metadata"]["name"]]


def test_pod_run_skips_npm_ci_skip_and_stamps(tmp_path):
    repo = make_repo(tmp_path)
    kube = FakeKube()
    ex = executor(kube)
    rc, _ = _run_verify_command(repo, "npm ci && npm test", 60, str(repo), "abc123", ex)
    assert rc == 0
    assert [a[5] for a, _ in kube.execs if is_run(a)] == ["npm ci && npm test"]
    assert not list(repo.rglob(".fubala-lock-stamp*"))


def test_transport_failure_deletes_pod_and_reruns_in_process(tmp_path):
    repo = make_repo(tmp_path)
    kube = FakeKube()
    ex = executor(kube)
    ex.prepare()
    kube.exec_error = ConnectionError("pod gone")
    rc, out = _run_verify_command(repo, "echo again", 10, str(repo), "abc123", ex)
    assert rc == 0 and "again" in out
    assert kube.deleted and ex.disabled


def test_exec_timeout_returns_124_and_deletes_pod(tmp_path):
    repo = make_repo(tmp_path)
    kube = FakeKube()
    ex = executor(kube)
    ex.prepare()
    real_exec = kube.exec
    calls = []

    def exec_(name, argv, stdin, on_output, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            return real_exec(name, argv, stdin, on_output, timeout)
        raise TimeoutError("too slow")

    kube.exec = exec_
    rc, out = _run_verify_command(repo, "sleep 999", 5, str(repo), "abc123", ex)
    assert rc == 124 and out == "verify timed out after 5s"
    assert calls[1] == 5 + pods.EXEC_MARGIN
    assert kube.deleted and not ex.disabled


def test_missing_token_warns_once_and_runs_in_process(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(pods, "SA_DIR", tmp_path / "no-sa")
    caplog.set_level(logging.WARNING, logger="app.pods")
    for _ in range(2):
        lines: list[str] = []
        got = pods.build_executor("verify", "s", SETTINGS, story_log=lines.append)
        assert got is None
        assert any("running in-process" in line for line in lines)
    warnings = [r for r in caplog.records if "ServiceAccount token" in r.message]
    assert len(warnings) == 1


def test_pod_mode_logs_the_step2_line():
    lines: list[str] = []
    ex = pods.build_executor(
        "pod", "s", SETTINGS, story_log=lines.append, kube_factory=lambda: FakeKube()
    )
    assert ex is not None
    assert (
        "run_executor=pod: headless runs in the worker pod; "
        "pump tools stay in-process until step 2\n"
    ) in lines
    assert ex.mode == "pod"
    assert pods.build_executor("in_process", "s", SETTINGS) is None


def test_empty_worker_image_falls_back():
    assert pods.build_executor("verify", "s", {**SETTINGS, "worker_image": ""}) is None


# ---- kill hooks, sweeps, runner ---------------------------------------------------------


def test_kill_story_and_kill_all_delete_pods():
    kube = FakeKube()
    a, b = executor(kube), executor(kube)
    b.story_id = "other"
    a.prepare()
    b.prepare()
    procs.kill_story("abc123")
    deadline = time.monotonic() + 5
    while len(kube.deleted) < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert kube.deleted == [kube.created[0]["metadata"]["name"]]
    procs.kill_all()
    assert len(kube.deleted) == 2


def test_sweep_pods_spares_running_and_live():
    kube = FakeKube(listed=[
        {"name": "old", "story": "gone"},
        {"name": "busy", "story": "running1"},
    ])  # fmt: skip
    pods.set_kube_factory(lambda: kube)
    assert pods.sweep_pods({"running1"}) == ["old"]
    assert pods.sweep_pods(set()) == ["old", "busy"]


def test_toolbox_train_ci_does_not_cut_workers_on_a_dev_node(tmp_path, monkeypatch):
    from app import train

    monkeypatch.setattr(train, "train_ci_hold", lambda db: object())
    from sqlmodel import create_engine

    kube = FakeKube(node="dev1")
    ex = executor(kube)
    box = ToolBox(engine=create_engine("sqlite://"), project_root=str(tmp_path), executor=ex)
    cmd, note = box._train_ci_adjust("pytest -n 4")
    assert cmd == "pytest -n 4"
    assert "pod on dev node, workers not cut" in note

    local = FakeKube(node="stor01")
    box = ToolBox(
        engine=create_engine("sqlite://"), project_root=str(tmp_path),
        executor=executor(local),
    )  # fmt: skip
    cmd, note = box._train_ci_adjust("pytest -n 4")
    assert cmd == "pytest -n 1" and "1 worker" in note


def test_startup_pod_sweep_runs_before_the_first_tick(tmp_path, monkeypatch):
    from test_proc_groups import _make_runner

    order: list[str] = []
    kube = FakeKube(listed=[{"name": "stale", "story": "x"}])
    real_delete = kube.delete_pod
    kube.delete_pod = lambda n: (order.append(f"delete:{n}"), real_delete(n))[1]
    pods.set_kube_factory(lambda: kube)
    r = _make_runner(tmp_path)

    async def tick(max_parallel):
        order.append("tick")

    monkeypatch.setattr(r, "_tick", tick)

    async def scenario():
        await r.start()
        for _ in range(100):
            if "tick" in order:
                break
            await asyncio.sleep(0.02)
        await r.stop()

    asyncio.run(scenario())
    assert order[0] == "delete:stale" and "tick" in order


def test_make_executor_reads_settings_and_in_process_is_default(tmp_path, monkeypatch):
    from test_proc_groups import _make_runner

    r = _make_runner(tmp_path)

    async def scenario():
        loop = asyncio.get_running_loop()
        assert r._make_executor("s1", loop) is None  # default in_process
        settings_store.save_settings(r.engine, {"run_executor": "verify", "worker_image": "t1"})
        monkeypatch.setattr(pods, "preflight", lambda image: None)
        monkeypatch.setattr(pods, "_kube_factory", lambda: FakeKube())
        got = r._make_executor("s1", loop)
        assert got is not None and got.story_id == "s1"
        got.close()

    asyncio.run(scenario())


def test_publishing_from_the_verify_thread_goes_through_the_loop(tmp_path, monkeypatch):
    from test_proc_groups import _make_runner

    from app.events import bus

    r = _make_runner(tmp_path)
    settings_store.save_settings(r.engine, {"run_executor": "pod", "worker_image": "t1"})
    monkeypatch.setattr(pods, "preflight", lambda image: None)

    async def scenario():
        q = await bus.subscribe()
        ex = r._make_executor("s1", asyncio.get_running_loop())
        assert ex is not None
        await asyncio.to_thread(ex._log_line, "from a thread\n")
        payload = await asyncio.wait_for(q.get(), 2)
        payload2 = await asyncio.wait_for(q.get(), 2)
        bus.unsubscribe(q)
        return payload + payload2

    assert "from a thread" in asyncio.run(scenario())


# ---- settings API -----------------------------------------------------------------


def test_settings_api_accepts_and_rejects(client):
    body = client.get("/api/v1/settings").json()
    assert body["run_executor"] == "in_process" and body["worker_image"] == ""
    ok = client.patch(
        "/api/v1/settings", json={"run_executor": "verify", "worker_image": "20261102-7"}
    )
    assert ok.status_code == 200
    assert ok.json()["run_executor"] == "verify" and ok.json()["worker_image"] == "20261102-7"
    for bad in ({"run_executor": "kubectl"}, {"worker_image": "bad tag/../x"}):
        assert client.patch("/api/v1/settings", json=bad).status_code == 422


def test_close_does_not_wait_for_inflight_run():
    import threading

    class Blocking(FakeKube):
        def exec(self, name, argv, stdin, on_output, timeout):
            if entered.is_set():  # close()'s own kill/reap execs answer at once
                return 0, "", ""
            entered.set()
            release.wait(5)
            raise RuntimeError("pod deleted")

    entered, release = threading.Event(), threading.Event()
    kube = Blocking()
    ex = pods.PodExecutor("s1", {"worker_image": "t"}, kube_factory=lambda: kube)
    ex.prepare()
    t = threading.Thread(target=lambda: _swallow(ex), daemon=True)
    t.start()
    assert entered.wait(5)
    ex.close()  # must return while run() still holds the exec lock
    assert kube.deleted
    release.set()
    t.join(5)


def _swallow(ex):
    try:
        ex.run("true", "", None, 10)
    except Exception:  # noqa: BLE001
        pass


# ---- end-of-run reaping ------------------------------------------------------------


def is_run(argv):
    """The exec_argv() of a run (the sync script has the same shape)."""
    return len(argv) == 6 and argv[3] == "_" and argv[2] != pods.SYNC_SCRIPT


REAP_OUT = (
    "41\t120\tsleep\tsleep600\n"
    "garbage line\n"
    "x\ty\tz\tw\n"
    "42\t5\tsh\tsh -c curl -H sk-secret-1 host\n"
)


@pytest.fixture
def reports():
    got: list[tuple[str, str, list]] = []
    procs.set_reporter(lambda sid, reason, entries: got.append((sid, reason, entries)))
    yield got
    procs.set_reporter(None)


class ReapKube(FakeKube):
    """FakeKube that answers REAP_SCRIPT with `reap_out` and run execs with `run_rc`."""

    def __init__(self, *, reap_out=REAP_OUT, reap_rc=0, run_rc=0, **kw):
        super().__init__(**kw)
        self.reap_out, self.reap_rc, self.run_rc = reap_out, reap_rc, run_rc
        self.reap_error: Exception | None = None
        self.names: list[tuple[str, str]] = []

    def exec(self, name, argv, stdin, on_output, timeout):
        if argv[:3] == ["sh", "-c", pods.REAP_SCRIPT]:
            self.execs.append((argv, stdin))
            self.names.append((name, "reap"))
            self.reap_timeout = timeout
            if self.reap_error:
                raise self.reap_error
            return self.reap_rc, self.reap_out, ""
        if is_run(argv):
            self.execs.append((argv, stdin))
            self.names.append((name, "run"))
            return self.run_rc, "out\n", ""
        return super().exec(name, argv, stdin, on_output, timeout)


def _verify(tmp_path, kube, ex=None):
    repo = make_repo(tmp_path)
    ex = ex or executor(kube)
    return ex, _run_verify_command(repo, "make test", 60, str(repo), "abc123", ex)


@pytest.mark.parametrize("run_rc,reason", [(0, "round_end"), (3, "round_end"), (124, "timeout")])
def test_run_is_followed_by_a_reap_that_becomes_events(tmp_path, reports, run_rc, reason):
    kube = ReapKube(run_rc=run_rc)
    ex, (rc, out) = _verify(tmp_path, kube)
    assert rc == run_rc and out.startswith("out")
    assert kube.execs[-1][0] == ["sh", "-c", pods.REAP_SCRIPT]
    assert is_run(kube.execs[-2][0])  # the reap comes right after the run exec
    assert kube.reap_timeout == pods.REAP_TIMEOUT
    assert reports == [
        (
            "abc123",
            reason,
            [
                {"command": "sleep (sleep600)", "pid": 41, "age_s": 120},
                {"command": "sh (sh -c curl -H sk-secret-1 host)", "pid": 42, "age_s": 5},
            ],
        )
    ]
    # the reap goes to this executor's pod only
    assert {n for n, _ in kube.names} == {ex.pod_name}


def test_reap_redacts_the_commands(tmp_path):
    out = pods.parse_reap_output(REAP_OUT, ("sk-secret-1",))
    assert "sk-secret-1" not in repr(out)
    assert out[0]["pid"] == 41 and len(out) == 2


def test_exec_timeout_reaps_before_the_pod_is_deleted(tmp_path, reports):
    kube = ReapKube()
    ex = executor(kube)
    ex.prepare()
    order: list[str] = []
    real = kube.exec

    def exec_(name, argv, stdin, on_output, timeout):
        if is_run(argv):
            raise TimeoutError("too slow")
        if argv[:3] == ["sh", "-c", pods.REAP_SCRIPT]:
            order.append("reap")
        return real(name, argv, stdin, on_output, timeout)

    kube.exec = exec_
    kube.delete_pod = lambda name: order.append("delete")
    repo = make_repo(tmp_path)
    rc, out = _run_verify_command(repo, "sleep 999", 5, str(repo), "abc123", ex)
    assert rc == 124 and out == "verify timed out after 5s"
    assert order == ["reap", "delete"]
    assert [r[1] for r in reports] == ["timeout"]


@pytest.mark.parametrize("failure", ["raises", "rc", "garbage"])
def test_a_failing_reap_never_changes_the_result(tmp_path, reports, failure):
    kube = ReapKube(run_rc=7)
    if failure == "raises":
        kube.reap_error = ConnectionError("stream reset")
    elif failure == "rc":
        kube.reap_rc = 1
    else:
        kube.reap_out = "\x00\xff nonsense\n\t\t\n"
    ex, (rc, out) = _verify(tmp_path, kube)
    assert rc == 7 and out.startswith("out")
    assert reports == []
    assert not ex.disabled and not kube.deleted


def test_a_failing_reporter_never_raises(tmp_path):
    def boom(*a):
        raise RuntimeError("db locked")

    procs.set_reporter(boom)
    try:
        ex, (rc, _out) = _verify(tmp_path, ReapKube())
    finally:
        procs.set_reporter(None)
    assert rc == 0


def test_close_mid_exec_reaps_with_cancel_before_the_delete(reports):
    import threading

    entered, release = threading.Event(), threading.Event()
    order: list[str] = []

    class Mid(ReapKube):
        def exec(self, name, argv, stdin, on_output, timeout):
            if is_run(argv):
                entered.set()
                release.wait(5)
                raise RuntimeError("pod deleted")
            if argv[:3] == ["sh", "-c", pods.REAP_SCRIPT]:
                order.append("reap")
            elif argv[:3] == ["sh", "-c", pods.KILL_SCRIPT]:
                order.append("kill")
            return super().exec(name, argv, stdin, on_output, timeout)

        def delete_pod(self, name):
            order.append("delete")
            super().delete_pod(name)

    kube = Mid()
    ex = pods.PodExecutor("abc123", {"worker_image": "t"}, kube_factory=lambda: kube)
    ex.prepare()
    t = threading.Thread(target=lambda: _swallow(ex), daemon=True)
    t.start()
    assert entered.wait(5)
    ex.close()
    release.set()
    t.join(5)
    assert order == ["kill", "reap", "delete"]
    assert kube.reap_timeout == pods.CLOSE_REAP_TIMEOUT
    assert [r[1] for r in reports] == ["cancel"]


def test_close_when_idle_does_not_reap():
    kube = ReapKube()
    ex = executor(kube)
    ex.prepare()
    ex.close()
    assert not any(a[:3] == ["sh", "-c", pods.REAP_SCRIPT] for a, _ in kube.execs)


# ---- the real reap script against a fake /proc ---------------------------------------


def _fake_proc(root: Path, pid: int, comm: str, state: str, sid: int, cmdline: str, start=100):
    d = root / str(pid)
    d.mkdir()
    # fields after the comm: state ppid pgrp session tty tpgid flags 4x faults 4x times
    # priority nice threads itrealvalue starttime + padding
    tail = f"{state} 0 {pid} {sid} 0 0 0 0 0 0 0 0 0 0 0 0 1 0 {start} 0 0 0"
    (d / "stat").write_text(f"{pid} ({comm}) {tail}\n")
    (d / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode() + b"\0" if cmdline else b"")


def test_reap_script_spares_the_watchdog_heartbeat_and_zombies(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    (root / "uptime").write_text("1000.50 2000.00\n")
    _fake_proc(root, 1, "sh", "S", 1, "sh -c watchdog")
    _fake_proc(root, 7, "sleep", "S", 1, "sleep 30")  # the watchdog's child, session 1
    _fake_proc(root, 20, "defunct", "Z", 20, "")
    _fake_proc(root, 21, "touch", "S", 21, f"touch {pods.HEARTBEAT}")
    _fake_proc(root, 30, "sl (eep) x", "S", 30, "sleep 600", start=500)  # the escapee
    killed = tmp_path / "killed"
    fake_kill = tmp_path / "fakekill"
    fake_kill.write_text(f'#!/bin/sh\necho "$@" >>{killed}\n')
    fake_kill.chmod(0o755)
    script = pods.reap_script(proc=str(root), kill=str(fake_kill), grace=0)
    res = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert len(lines) == 1, res.stdout
    pid, age, comm, argv = lines[0].split("\t")
    assert (pid, comm) == ("30", "sl (eep) x")  # stat parsed after the LAST ')'
    assert int(age) == 1000 - 500 // 100 or int(age) >= 990
    assert "sleep" in argv
    assert killed.read_text().splitlines() == ["-TERM 30", "-KILL 30"]
    # the entry round-trips into a report entry
    assert pods.parse_reap_output(res.stdout)[0]["pid"] == 30


def test_reap_script_with_nothing_to_kill_prints_nothing(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    (root / "uptime").write_text("10.00 10.00\n")
    _fake_proc(root, 1, "sh", "S", 1, "sh")
    _fake_proc(root, 9, "sleep", "S", 1, "sleep 1")
    script = pods.reap_script(proc=str(root), kill="/bin/false", grace=0)
    res = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0 and res.stdout == ""
