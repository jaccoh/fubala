"""Parity guard: docker/worker.Dockerfile must pin the same node image, uv image
and claude CLI as the runtime Dockerfile, so a story round in a worker pod runs
the toolchain the runtime image would have used."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RUNTIME = REPO / "Dockerfile"
WORKER = REPO / "docker" / "worker.Dockerfile"
PYPROJECT = REPO / "backend" / "pyproject.toml"
CODE_GRAPH = REPO / "backend" / "app" / "code_graph.py"


def graph_pin_dockerfile(text: str) -> str | None:
    m = re.search(r"uv tool install code-review-graph==([^\s'\"]+)", text)
    return m.group(1) if m else None


def graph_pin_pyproject(text: str) -> str | None:
    m = re.search(r"^code-review-graph\s*=\s*[\"']([^\"']+)[\"']", text, re.M)
    return m.group(1) if m else None


def graph_pin_module(text: str) -> str | None:
    m = re.search(r"^PIN = [\"']([^\"']+)[\"']", text, re.M)
    return m.group(1) if m else None


def parse_pins(text: str) -> dict[str, str | None]:
    """NODE_IMAGE / UV_IMAGE defaults and the claude-code pin of a Dockerfile."""
    node = re.search(r"^ARG NODE_IMAGE=(\S+)", text, re.M)
    uv = re.search(r"^ARG UV_IMAGE=(\S+)", text, re.M)
    claude = re.search(r"@anthropic-ai/claude-code@([^\s'\"]+)", text)
    return {
        "NODE_IMAGE": node.group(1) if node else None,
        "UV_IMAGE": uv.group(1) if uv else None,
        "claude": claude.group(1) if claude else None,
    }


def mismatches(runtime: str, worker: str) -> list[str]:
    a, b = parse_pins(runtime), parse_pins(worker)
    return [k for k in a if a[k] is None or a[k] != b[k]]


def test_real_dockerfiles_match():
    pins = parse_pins(RUNTIME.read_text())
    assert all(pins.values()), pins
    assert mismatches(RUNTIME.read_text(), WORKER.read_text()) == []


@pytest.mark.parametrize("key", ["NODE_IMAGE", "UV_IMAGE", "claude"])
def test_mutated_pin_is_reported(key: str):
    # Independent of the real files agreeing: one file's text against a
    # mutated in-memory copy of itself.
    text = WORKER.read_text()
    old = parse_pins(text)[key]
    assert old
    mutated = text.replace(old, old + "-x")
    assert mutated != text
    assert mismatches(text, text) == []
    assert mismatches(text, mutated) == [key]


def test_worker_dockerfile_ends_with_user_10003():
    lines = [
        ln.strip()
        for ln in WORKER.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert lines[-1] == "USER 10003"


def test_code_review_graph_pins_match_pyproject():
    want = graph_pin_pyproject(PYPROJECT.read_text())
    assert want, "pyproject.toml [tool.fubala] must pin code-review-graph"
    assert graph_pin_dockerfile(WORKER.read_text()) == want
    assert graph_pin_dockerfile(RUNTIME.read_text()) == want
    assert graph_pin_module(CODE_GRAPH.read_text()) == want


def test_code_review_graph_pin_mismatch_is_reported():
    worker = WORKER.read_text()
    pin = graph_pin_dockerfile(worker)
    assert pin
    mutated = worker.replace(f"code-review-graph=={pin}", f"code-review-graph=={pin}.1")
    assert mutated != worker
    assert graph_pin_dockerfile(mutated) != graph_pin_pyproject(PYPROJECT.read_text())
