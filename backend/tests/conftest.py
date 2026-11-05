from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from app import settings_store
from app.main import create_app
from app.providers import Delta, Usage

# the shipped defaults, snapshotted before any fixture touches them
PRODUCT_DEFAULTS = dict(settings_store.DEFAULTS)

# Smallest text that passes Runner's REQUIRED_SPEC_SECTIONS validation
# ("## Goal", "## Implementation steps", "## Acceptance checks") — shared by
# the fake gateways so every spec-phase test stays green under the
# deliverable guard.
MINIMAL_SPEC = (
    "## Goal\nAdd the thing.\n\n"
    "## Implementation steps\n1. Do it\n\n"
    "## Acceptance checks\n- [ ] tests pass\n"
)

# prompts the story runner sends for its phases
STORY_PHASE_MARKERS = (
    "=== SPEC REQUEST ===",
    "=== AI REVIEW REQUEST ===",
    "=== CODE REQUEST ===",
    "=== CODE REVIEW REQUEST ===",
)

SPEC_MARKER = STORY_PHASE_MARKERS[0]


def _is_story_phase(content: object) -> bool:
    return isinstance(content, str) and any(
        content.startswith(marker) for marker in STORY_PHASE_MARKERS
    )


class FakeGateway:
    """Deterministic gateway for tests; records every call.

    Story-phase prompts (=== SPEC REQUEST === etc.) get a spec that satisfies
    the runner's REQUIRED_SPEC_SECTIONS validation; everything else replays
    `chunks`, defaulting to a plain chat reply.
    """

    def __init__(self, chunks: list[str] | None = None):
        self.chunks = chunks or ["Hello", " there", "!"]
        self.calls: list[dict] = []

    async def stream_chat(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list | None = None,
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[object]:
        self.calls.append({"model": model, "messages": messages})
        last = messages[-1].get("content") if messages else ""
        if isinstance(last, str) and last.startswith(SPEC_MARKER):
            # spec phases must satisfy the runner's section validation
            yield Delta(text=MINIMAL_SPEC)
        else:
            for chunk in self.chunks:
                yield Delta(text=chunk)
        yield Usage(prompt_tokens=3, completion_tokens=5)


@pytest.fixture(autouse=True)
def _phase_models_follow_the_story(monkeypatch):
    """The shipped spec_model default names a claude-max model, which test
    installs have no credentials for. Every test runs the spec on the story's
    own model unless it sets spec_model itself; PRODUCT_DEFAULTS has the real
    default for the tests that assert it.

    An ambient Claude Max token (a dev box or a pipeline sandbox exports one)
    would also make claude-max the default model and send story phases to a
    real `claude`; tests that want the provider key it explicitly."""
    monkeypatch.setitem(settings_store.DEFAULTS, "spec_model", "")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


# env the app reads to decide it runs inside a cluster
IN_CLUSTER_ENV = (
    "KUBERNETES_SERVICE_HOST",
    "KUBERNETES_SERVICE_PORT",
    "NODE_NAME",
    "POD_NAME",
    "POD_UID",
)


@pytest.fixture(autouse=True)
def _tests_never_reach_a_cluster(monkeypatch, tmp_path_factory):
    """The CI test pod runs in-cluster with a mounted ServiceAccount token and
    the kubernetes env; without isolation the app under test would list pods
    on the real API (a 403 in the log) and take in-cluster paths that a laptop
    never takes. Tests that want the pod path set the env themselves and
    inject a fake KubeApi."""
    from app import pods

    for var in IN_CLUSTER_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(pods, "SA_DIR", tmp_path_factory.mktemp("no-serviceaccount"))


@pytest.fixture(autouse=True)
def _git_identity_is_the_suites_own(monkeypatch, tmp_path_factory):
    """Product merges (train.py, workspaces.merge) run `git merge --no-ff`
    without an identity. A dev laptop has a global gitconfig; a worker pod
    (HOME=/cache/home, UID 10003) has none, so every merge died with
    "Committer identity unknown". Give git a suite-owned global config and
    ignore the host's system config so results do not depend on where the
    suite runs. GIT_CONFIG_GLOBAL, not HOME, so uv/.claude paths stay put."""
    cfg = tmp_path_factory.mktemp("gitconfig") / "gitconfig"
    cfg.write_text("[user]\n\tname = fubala-tests\n\temail = tests@fubala.invalid\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def client(tmp_path, monkeypatch, gateway) -> TestClient:
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "test.db"))
    app = create_app(gateway=gateway)
    with TestClient(app) as c:
        c.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
        yield c


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in text.strip().split("\n\n"):
        event, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                import json

                data = json.loads(line[len("data: ") :])
        if event is not None:
            events.append((event, data or {}))
    return events
