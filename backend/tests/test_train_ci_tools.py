"""Coder in-round test tools run with one worker while a train's CI is pending."""
import pytest
from sqlmodel import create_engine

from app import runner, train
from app.agent_tools import ToolBox, single_worker_command

NOTICE = "train CI running: tests with 1 worker"


@pytest.fixture
def calls(monkeypatch):
    seen: list[str] = []

    def fake(workspace, command, *a, **k):
        seen.append(command)
        return 0, "ok"

    monkeypatch.setattr(runner, "_run_verify_command", fake)
    return seen


def _box(tmp_path, verify, test=None):
    return ToolBox(
        engine=create_engine("sqlite://"),
        project_root=str(tmp_path),
        writable=True,
        verify_command=verify,
        test_command=test or verify,
    )


def _hold(monkeypatch, value):
    monkeypatch.setattr(train, "train_ci_hold", lambda db: value)


def _both(box):
    return [box._run_verify({}), box._run_tests({})]


@pytest.mark.parametrize(
    "cmd,want",
    [
        ("uv run pytest -q -n auto", "uv run pytest -q -n 1"),
        ("pytest --numprocesses=4", "pytest --numprocesses=1"),
        ("env TEST_WORKERS=2 sh tools/test.sh", "env TEST_WORKERS=1 sh tools/test.sh"),
    ],
)
def test_hold_rewrites(tmp_path, monkeypatch, calls, cmd, want):
    _hold(monkeypatch, object())
    for res in _both(_box(tmp_path, cmd)):
        assert res["command"] == want
        assert res["output"].startswith(NOTICE)
    assert calls == [want, want]


def test_hold_paths_appended(tmp_path, monkeypatch, calls):
    _hold(monkeypatch, object())
    box = _box(tmp_path, "x", "TEST_WORKERS=4 sh tools/test.sh")
    box._run_tests({"paths": ["tests/x.py"]})
    assert calls == ["TEST_WORKERS=1 sh tools/test.sh tests/x.py"]


def test_no_hold_unchanged(tmp_path, monkeypatch, calls):
    _hold(monkeypatch, None)
    box = _box(tmp_path, "uv run pytest -n auto")
    for res in _both(box):
        assert res["command"] == "uv run pytest -n auto"
        assert res["output"] == "ok"
    assert calls == ["uv run pytest -n auto"] * 2


@pytest.mark.parametrize("cmd", ["pytest -q", "sh tools/test.sh"])
def test_hold_nothing_to_rewrite(tmp_path, monkeypatch, calls, cmd):
    _hold(monkeypatch, object())
    for res in _both(_box(tmp_path, cmd)):
        assert res["command"] == cmd
        assert "worker reduction skipped" in res["output"]
    assert calls == [cmd, cmd]


def test_hold_check_failure_is_ignored(tmp_path, monkeypatch, calls):
    def boom(db):
        raise RuntimeError("db gone")

    monkeypatch.setattr(train, "train_ci_hold", boom)
    res = _box(tmp_path, "pytest -n 4")._run_verify({})
    assert res["command"] == "pytest -n 4"
    assert res["output"] == "ok"


@pytest.mark.parametrize(
    "cmd,want",
    [
        ("head -n 5 x && pytest -n 8", "head -n 5 x && pytest -n 1"),
        ("pytest -n4", "pytest -n1"),
        ("pytest -n=3 | tee out", "pytest -n=1 | tee out"),
        ("pytest --numprocesses 2", "pytest --numprocesses 1"),
        ("sed -n 1p f; pytest", "sed -n 1p f; pytest"),
        ("MY_TEST_WORKERS=2 pytest", "MY_TEST_WORKERS=2 pytest"),
        ("pytest -q", "pytest -q"),
        ("a && TEST_WORKERS=3 b", "a && TEST_WORKERS=1 b"),
    ],
)
def test_single_worker_command(cmd, want):
    new, changed = single_worker_command(cmd)
    assert new == want
    assert changed == (want != cmd)
