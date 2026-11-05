"""Provider CRUD, key handling, and skill/cost routing."""

import json
import logging
import time

from conftest import MINIMAL_SPEC

from app import registry as registry_module
from app.registry import UnknownModelError


def by_id(providers: list[dict]) -> dict[str, dict]:
    return {p["id"]: p for p in providers}


def dead_ollama(monkeypatch) -> None:
    """Pin the seeded ollama endpoint as probed-unreachable.

    Routing only picks a credential-less openai_compatible provider when its
    base_url answers a probe; stamping the probe cache keeps that decision
    deterministic (and instant) even on a machine that really runs ollama
    on :11434."""
    monkeypatch.setitem(
        registry_module._PROBE_CACHE,
        "http://localhost:11434/v1",
        (time.monotonic(), False),
    )


def test_seed_provides_defaults_and_models_endpoint(client):
    res = client.get("/api/v1/models")
    assert res.status_code == 200
    body = res.json()
    assert body["default_model"] == "echo/dev"

    providers = by_id(body["providers"])
    assert providers["echo"]["configured"] is True
    assert providers["echo"]["protected"] is True
    assert {"id": "echo/dev", "model_id": "dev", "skill": "low", "cost": "low"} in [
        {k: m[k] for k in ("id", "model_id", "skill", "cost")}
        for m in providers["echo"]["models"]
    ]
    # seeded but keyless
    assert providers["anthropic"]["configured"] is False
    assert providers["zai"]["base_url"] == "https://api.z.ai/api/paas/v4"
    assert providers["ollama"]["configured"] is True  # keyless local endpoint


def test_preset_dropdown_endpoint(client):
    presets = {p["id"]: p for p in client.get("/api/v1/providers/presets").json()}
    assert set(presets) >= {
        "openai", "anthropic", "zai", "openrouter", "gemini", "ollama", "custom"
    }
    assert presets["openai"]["env_key"] == "OPENAI_API_KEY"
    assert presets["custom"]["kind"] == "openai_compatible"


def test_create_provider_from_preset_with_overrides(client):
    res = client.post(
        "/api/v1/providers",
        json={"preset": "openai", "id": "openai-eu", "base_url": "https://eu.example/v1"},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["id"] == "openai-eu"
    assert body["display_name"] == "OpenAI"
    assert body["base_url"] == "https://eu.example/v1"
    assert body["configured"] is False  # no key yet
    assert any(m["model_id"] == "gpt-5.2" for m in body["models"])

    # duplicate ids rejected, unknown presets rejected
    assert client.post("/api/v1/providers", json={"preset": "openai"}).status_code == 409
    assert client.post("/api/v1/providers", json={"preset": "nope"}).status_code == 400


def test_key_storage_and_masking(client):
    res = client.post("/api/v1/providers", json={"preset": "anthropic", "id": "anthropic-direct"})
    assert res.json()["has_key"] is False
    pid = res.json()["id"]

    res = client.post(f"/api/v1/providers/{pid}/key", json={"key": "sk-ant-SECRET123"})
    body = res.json()
    assert body["has_key"] is True
    assert body["key_hint"] == "T123"
    assert "SECRET" not in str(body)  # never echoed back
    assert body["configured"] is True

    # clearing works
    body = client.post(f"/api/v1/providers/{pid}/key", json={"key": ""}).json()
    assert body["has_key"] is False


def test_limit_reset_tz_zone_defaults_and_patch(client):
    from app.models import Provider
    from app.registry import limit_reset_zone

    # zone defaults: zai states resets in UTC+8, everyone else UTC
    assert limit_reset_zone(Provider(id="zai", display_name="z", kind="litellm")) == (
        "Asia/Shanghai"
    )
    assert limit_reset_zone(
        Provider(id="zai-plan", display_name="z", kind="litellm")
    ) == "Asia/Shanghai"
    assert limit_reset_zone(Provider(id="openai", display_name="o", kind="litellm")) == "UTC"
    # an explicit row setting always wins
    assert limit_reset_zone(
        Provider(id="zai", display_name="z", kind="litellm", limit_reset_tz="Europe/Amsterdam")
    ) == "Europe/Amsterdam"

    res = client.patch("/api/v1/providers/zai", json={"limit_reset_tz": "UTC"})
    assert res.status_code == 200, res.text
    assert res.json()["limit_reset_tz"] == "UTC"
    # an unknown zone is rejected, the row keeps the last good value
    assert (
        client.patch(
            "/api/v1/providers/zai", json={"limit_reset_tz": "Mars/Olympus"}
        ).status_code
        == 400
    )
    assert client.get("/api/v1/providers").json() is not None
    providers = by_id(client.get("/api/v1/providers").json())
    assert providers["zai"]["limit_reset_tz"] == "UTC"
    # empty string clears back to the zone default
    res = client.patch("/api/v1/providers/zai", json={"limit_reset_tz": ""})
    assert res.status_code == 200
    assert res.json()["limit_reset_tz"] is None


def test_provider_limit_hold_endpoints(client):
    runner = client.app.state.story_runner
    assert client.get("/api/v1/providers/limits").json() == []
    assert client.post("/api/v1/providers/zai/limits/clear").status_code == 404

    from datetime import UTC, datetime, timedelta

    until = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
    runner.set_provider_hold("zai", until, "rate limit (headless): usage limit")
    holds = client.get("/api/v1/providers/limits").json()
    assert len(holds) == 1
    assert holds[0]["provider_id"] == "zai"
    assert datetime.fromisoformat(holds[0]["until"]) == until
    assert "usage limit" in holds[0]["reason"]

    assert client.post("/api/v1/providers/zai/limits/clear").status_code == 204
    assert client.get("/api/v1/providers/limits").json() == []
    assert client.post("/api/v1/providers/zai/limits/clear").status_code == 404


def test_model_crud_and_tier_validation(client):
    res = client.post("/api/v1/providers", json={"preset": "custom", "id": "my-proxy"})

    res = client.post(
        "/api/v1/providers/my-proxy/models",
        json={"model_id": "some-model", "skill": "high", "cost": "low"},
    )
    assert res.status_code == 201
    model = res.json()["models"][-1]
    assert model["model_id"] == "some-model"

    providers = by_id(client.get("/api/v1/providers").json())
    row = [m for m in providers["my-proxy"]["models"] if m["model_id"] == "some-model"][0]
    assert row["skill"] == "high" and row["cost"] == "low"

    # invalid tiers rejected
    assert (
        client.post(
            "/api/v1/providers/my-proxy/models",
            json={"model_id": "x", "skill": "ultra"},
        ).status_code
        == 400
    )

    # delete the model
    assert client.delete("/api/v1/providers/my-proxy").status_code == 204


def test_delete_protected_provider_rejected(client):
    assert client.delete("/api/v1/providers/echo").status_code == 409
    assert client.delete("/api/v1/providers/anthropic").status_code == 204


def test_skill_routing_picks_cheapest_qualifying(client, monkeypatch, caplog):
    registry = client.app.state.registry
    dead_ollama(monkeypatch)

    # with no keys configured, only echo (low/low) qualifies: keyless ollama
    # is skipped because nothing answers at its base_url
    assert registry.resolve_for_skill("low").canonical == "echo/dev"

    # medium: ollama's qwen2.5-coder is unreachable, so zai's keyed glm wins
    client.post("/api/v1/providers/zai/key", json={"key": "zai-key"})
    with caplog.at_level(logging.WARNING, logger="app.registry"):
        resolved = registry.resolve_for_skill("medium")
    assert resolved.canonical == "zai/glm-4.7"
    assert resolved.skill == "medium"
    # the skip is named in the log, model + reason
    skips = [
        m for m in caplog.messages if m.startswith("routing: skipping")
    ]
    assert any(
        "ollama/qwen2.5-coder" in s and "unreachable" in s for s in skips
    ), skips

    # high skill: nothing qualifies yet -> error
    try:
        registry.resolve_for_skill("high")
        raise AssertionError("expected UnknownModelError")
    except UnknownModelError:
        pass

    # enable a high-skill provider and high routes to it
    client.post("/api/v1/providers", json={"preset": "anthropic", "id": "anthropic-direct"})
    client.post("/api/v1/providers/anthropic-direct/key", json={"key": "sk"})
    resolved = registry.resolve_for_skill("high")
    assert resolved.canonical == "anthropic-direct/claude-opus-4-6"

    # a disabled provider is skipped even with a key
    client.patch("/api/v1/providers/anthropic-direct", json={"enabled": False})
    try:
        registry.resolve_for_skill("high")
        raise AssertionError("expected UnknownModelError after disable")
    except UnknownModelError:
        pass


def test_story_can_request_skill_instead_of_model(client, monkeypatch):
    dead_ollama(monkeypatch)
    client.post("/api/v1/providers/zai/key", json={"key": "zai-key"})
    res = client.post(
        "/api/v1/stories", json={"title": "Auto skill story", "skill": "medium"}
    )
    assert res.status_code == 201
    story = res.json()
    assert story["model"] is None
    assert story["skill"] == "medium"

    # queue it; the scheduler stamps the routed model
    client.post(f"/api/v1/stories/{story['id']}/queue")
    import time

    deadline = time.time() + 30
    while time.time() < deadline:
        current = client.get(f"/api/v1/stories/{story['id']}").json()
        if current["status"] in ("human_review", "failed"):
            break
        time.sleep(0.05)
    # keyless ollama is unreachable, so the keyed zai model is the one stamped
    assert current["model"] == "zai/glm-4.7"
    events = [e["kind"] for e in current["events"]]
    assert "note" in events  # the routing note


def test_story_with_no_usable_model_stays_queued(client, monkeypatch):
    """Every candidate unusable -> a routing problem, not a story failure:
    the story stays queued with the reason on error and no model stamped."""
    dead_ollama(monkeypatch)

    res = client.post(
        "/api/v1/stories", json={"title": "No usable model", "skill": "high"}
    )
    assert res.status_code == 201
    story = res.json()
    client.post(f"/api/v1/stories/{story['id']}/queue")

    deadline = time.time() + 30
    current = story
    while time.time() < deadline:
        current = client.get(f"/api/v1/stories/{story['id']}").json()
        if current["error"]:
            break
        time.sleep(0.05)

    assert current["status"] == "queued"  # never in_progress, never failed
    assert current["model"] is None
    assert "no usable model for skill high" in current["error"]
    notes = [
        e["data"]
        for e in current["events"]
        if "no usable model for skill high" in e["data"]
    ]
    assert notes, "expected a routing-error note event"

    # other stories keep flowing: a low-skill story still routes to echo
    other = client.post("/api/v1/stories", json={"title": "Low skill", "skill": "low"})
    other_id = other.json()["id"]
    client.post(f"/api/v1/stories/{other_id}/queue")
    import time as t

    deadline = t.time() + 30
    while t.time() < deadline:
        low = client.get(f"/api/v1/stories/{other_id}").json()
        if low["status"] in ("human_review", "failed"):
            break
        t.sleep(0.05)
    assert low["model"] == "echo/dev"


def test_infra_failure_keeps_model_and_retry_reruns_on_same_model(
    tmp_path, monkeypatch
):
    """An infrastructure failure on a keyed provider KEEPS story.model: the
    retry re-runs the coder on the same model instead of being re-routed to
    whatever is cheapest.

    skill=low against a pinned medium model is the mutation check: under the
    old contract (_fail_infrastructure clearing story.model) the retry would
    re-route per skill and land on echo/dev — a silent downgrade."""
    import subprocess

    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers import Delta, GatewayError, ToolCall

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )

    class ExpiredTokenGateway:
        """Spec/ai-review/review succeed; the coder call dies on auth."""

        def __init__(self):
            self.calls: list[dict] = []

        async def stream_chat(
            self, *, model, messages, tools=None, max_output_tokens=None
        ):
            self.calls.append({"model": model, "tools": tools})
            n = len(self.calls)
            if n == 1:  # spec
                yield Delta(text=MINIMAL_SPEC)
            elif n == 2:  # ai review of the spec
                yield Delta(text="fine\n\nVERDICT: APPROVED")
            elif n == 3:  # coder attempt: the key is present but expired
                raise GatewayError("[zai] AuthenticationError: expired token")
            elif n == 4:  # retry coder run: must be the SAME model
                assert any(t["function"]["name"] == "write_file" for t in tools or [])
                yield ToolCall(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps(
                        {"path": "infra.py", "content": "X = 1\n"}
                    ),
                )
            elif n == 5:
                yield Delta(text="wrote infra.py")
            elif n == 6:  # code review
                yield Delta(text="diff matches spec\n\nVERDICT: APPROVED")

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "infra.db"))
    gateway = ExpiredTokenGateway()
    app = create_app(gateway=gateway)
    with TestClient(app) as c:
        c.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": str(repo),
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        # the provider stays keyed and therefore usable: an expired token is
        # a story-visible failure, not a routing problem, so the pinned model
        # must survive both the failure and the retry
        c.post("/api/v1/providers/zai/key", json={"key": "zai-key"})
        story = c.post(
            "/api/v1/stories",
            json={"title": "Infra failure", "model": "zai/glm-4.7", "skill": "low"},
        ).json()
        c.post(f"/api/v1/stories/{story['id']}/queue")
        approved = wait_for_status(c, story["id"], "human_review", timeout=30)
        spec_before = approved["spec"]
        c.post(f"/api/v1/stories/{story['id']}/approve", json={"by": "t"})

        failed = wait_for_status(c, story["id"], "failed", timeout=30)
        assert failed["error"].startswith("infrastructure: ")
        assert "expired token" in failed["error"]
        assert failed["spec"] == spec_before  # untouched: no re-spec
        assert failed["base_ref"]  # kept: the worktree survives for retry
        assert failed["model"] == "zai/glm-4.7"  # KEPT: no silent downgrade
        infra_notes = [
            json.loads(e["data"])
            for e in failed["events"]
            if '"infra": true' in e["data"].lower()
        ]
        assert infra_notes, "expected an infra-marked note event"
        assert infra_notes[0]["model"] == "zai/glm-4.7"

        # retry at stage=queued re-runs the coder on the SAME model without
        # re-speccing; recovery from a broken key is fixing/PATCHing it
        c.post(f"/api/v1/stories/{story['id']}/retry", json={"stage": "queued"})
        retried = wait_for_status(c, story["id"], "human_review", timeout=30)
        assert retried["spec"] == spec_before
        assert retried["spec_version"] == approved["spec_version"]
        assert retried["model"] == "zai/glm-4.7"
        # every gateway call went out with the pinned model, the retry's
        # coder round included
        assert gateway.calls and all(
            call["model"] == "zai/glm-4.7" for call in gateway.calls
        )
        # kept, not replaced: no reroute note names a predecessor
        replaced = [
            json.loads(e["data"])
            for e in retried["events"]
            if '"replaced"' in e["data"]
        ]
        assert not replaced


def _coder_repo(tmp_path):
    """A one-commit git repo the coder's worktree can fork from."""
    import subprocess

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "README.md").write_text("# demo\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )
    return repo


def _notes_with(story_json: dict, marker: str) -> list[dict]:
    """Parsed note-event payloads whose raw data mentions `marker`."""
    return [
        json.loads(e["data"])
        for e in story_json["events"]
        if marker in e["data"]
    ]


def test_unusable_model_reroutes_at_old_models_floor_never_cheaper(
    tmp_path, monkeypatch
):
    """A story whose pinned model row is DELETED re-routes, but only to a
    model whose skill tier is >= the old model's tier — never echo/dev.

    The old model is zai/glm-4.7 (skill=medium); the cheaper keyed zai
    glm-4.7-flash and echo/dev are both low, so a floor at the old model's
    tier must land on the other medium model we seed."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers import Delta, ToolCall

    repo = _coder_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "reroute.db"))

    class CoderGateway:
        def __init__(self):
            self.calls: list[dict] = []

        async def stream_chat(
            self, *, model, messages, tools=None, max_output_tokens=None
        ):
            self.calls.append({"model": model, "tools": tools})
            n = len(self.calls)
            if n == 1:  # spec
                yield Delta(text=MINIMAL_SPEC)
            elif n == 2:  # ai review
                yield Delta(text="fine\n\nVERDICT: APPROVED")
            elif n == 3:  # coder round 1
                yield ToolCall(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps(
                        {"path": "reroute.py", "content": "Y = 2\n"}
                    ),
                )
            elif n == 4:
                yield Delta(text="wrote reroute.py")
            elif n == 5:  # code review
                yield Delta(text="diff matches spec\n\nVERDICT: APPROVED")

    gateway = CoderGateway()
    app = create_app(gateway=gateway)
    with TestClient(app) as c:
        c.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": str(repo),
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        # keyed zai with a second MEDIUM model as the replacement target
        c.post("/api/v1/providers/zai/key", json={"key": "zai-key"})
        c.post(
            "/api/v1/providers/zai/models",
            json={"model_id": "glm-5.3", "skill": "medium", "cost": "medium"},
        )
        # pin the model, then delete its row: the story now names a model the
        # registry no longer knows
        story = c.post(
            "/api/v1/stories",
            json={"title": "Deleted model", "model": "zai/glm-4.7"},
        ).json()
        zai_models = {
            m["model_id"]: m["row_id"]
            for p in c.get("/api/v1/providers").json()
            if p["id"] == "zai"
            for m in p["models"]
        }
        res = c.delete(f"/api/v1/providers/zai/models/{zai_models['glm-4.7']}")
        assert res.status_code == 204

        c.post(f"/api/v1/stories/{story['id']}/queue")
        current = wait_for_status(c, story["id"], "human_review", timeout=30)
        # re-routed to the other medium model, never the cheaper tiers
        assert current["model"] == "zai/glm-5.3"
        assert current["model"] != "echo/dev"
        assert current["model"] != "zai/glm-4.7-flash"
        reroutes = _notes_with(current, '"replaced"')
        assert reroutes, "expected a reroute note naming old and new"
        assert reroutes[0]["replaced"] == "zai/glm-4.7"
        assert reroutes[0]["routed"] == "zai/glm-5.3"
        assert reroutes[0]["reason"] == "unknown model"
        assert reroutes[0]["skill"] == "medium"  # the old model's tier
        # and the coder round actually ran on the replacement
        assert gateway.calls[-1]["model"] == "zai/glm-5.3"


def test_no_usable_model_at_floor_keeps_story_queued_and_board_flowing(
    tmp_path, monkeypatch
):
    """Nothing usable at the skill floor: a routing problem, not a story
    failure — the story stays queued with the reason on error and a note,
    while other stories keep flowing."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers import Delta, ToolCall

    repo = _coder_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "nofloor.db"))

    class CoderGateway:
        def __init__(self):
            self.calls: list[dict] = []

        async def stream_chat(
            self, *, model, messages, tools=None, max_output_tokens=None
        ):
            self.calls.append({"model": model, "tools": tools})
            n = len(self.calls)
            if n == 1:  # spec
                yield Delta(text=MINIMAL_SPEC)
            elif n == 2:  # ai review
                yield Delta(text="fine\n\nVERDICT: APPROVED")
            elif n == 3:  # coder round 1 (the OTHER story)
                yield ToolCall(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps(
                        {"path": "other.py", "content": "Z = 3\n"}
                    ),
                )
            elif n == 4:
                yield Delta(text="wrote other.py")
            elif n == 5:  # code review
                yield Delta(text="diff matches spec\n\nVERDICT: APPROVED")

    gateway = CoderGateway()
    app = create_app(gateway=gateway)
    with TestClient(app) as c:
        c.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": str(repo),
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        # a keyed high-skill pin whose provider is then disabled: unusable,
        # and nothing else in the registry has a high skill tier
        c.post("/api/v1/providers/anthropic/key", json={"key": "sk-ant"})
        story = c.post(
            "/api/v1/stories",
            json={"title": "High floor", "model": "anthropic/claude-opus-4-6"},
        ).json()
        c.patch("/api/v1/providers/anthropic", json={"enabled": False})
        c.post(f"/api/v1/stories/{story['id']}/queue")

        import time

        deadline = time.time() + 30
        current = None
        while time.time() < deadline:
            current = c.get(f"/api/v1/stories/{story['id']}").json()
            if current.get("error"):
                break
            time.sleep(0.05)

        assert current["status"] == "queued"  # never failed, never in flight
        assert "no usable model for skill high" in current["error"]
        assert current["model"] == "anthropic/claude-opus-4-6"  # not replaced
        notes = _notes_with(current, "no usable model for skill high")
        assert notes, "expected a routing-error note event"
        assert notes[0]["reason"] == "provider disabled"
        assert notes[0]["model"] == "anthropic/claude-opus-4-6"

        # the scheduler is not blocked: a low-skill story still runs to the
        # human gate on echo
        other = c.post("/api/v1/stories", json={"title": "Flowing", "skill": "low"})
        other_id = other.json()["id"]
        c.post(f"/api/v1/stories/{other_id}/queue")
        done = wait_for_status(c, other_id, "human_review", timeout=30)
        assert done["model"] == "echo/dev"


def test_deleted_provider_row_falls_back_to_story_skill_floor(
    tmp_path, monkeypatch
):
    """The pinned model's PROVIDER row is gone: its tier dies with it, so the
    floor falls back to the story's own skill (medium here) and the reroute
    note records that fallback tier."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.providers import Delta, ToolCall

    repo = _coder_repo(tmp_path)
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "fallback.db"))

    class CoderGateway:
        def __init__(self):
            self.calls: list[dict] = []

        async def stream_chat(
            self, *, model, messages, tools=None, max_output_tokens=None
        ):
            self.calls.append({"model": model, "tools": tools})
            n = len(self.calls)
            if n == 1:  # spec
                yield Delta(text=MINIMAL_SPEC)
            elif n == 2:  # ai review
                yield Delta(text="fine\n\nVERDICT: APPROVED")
            elif n == 3:  # coder round 1
                yield ToolCall(
                    id="w1",
                    name="write_file",
                    arguments=json.dumps(
                        {"path": "fallback.py", "content": "W = 4\n"}
                    ),
                )
            elif n == 4:
                yield Delta(text="wrote fallback.py")
            elif n == 5:  # code review
                yield Delta(text="diff matches spec\n\nVERDICT: APPROVED")

    gateway = CoderGateway()
    app = create_app(gateway=gateway)
    with TestClient(app) as c:
        c.patch(
            "/api/v1/settings",
            json={
                "story_poll_interval": 0.05,
                "project_root": str(repo),
                "write_tools_enabled": True,
                "max_tool_calls": 8,
            },
        )
        # a keyed medium pin on ollama (its qwen2.5-coder is medium/low); the
        # story's own skill is also medium, so the fallback floor is medium
        # and the keyed zai medium model is the target
        c.post("/api/v1/providers/ollama/key", json={"key": "ollama-key"})
        c.post("/api/v1/providers/zai/key", json={"key": "zai-key"})
        story = c.post(
            "/api/v1/stories",
            json={
                "title": "Deleted provider",
                "model": "ollama/qwen2.5-coder",
                "skill": "medium",
            },
        ).json()
        assert c.delete("/api/v1/providers/ollama").status_code == 204

        c.post(f"/api/v1/stories/{story['id']}/queue")
        current = wait_for_status(c, story["id"], "human_review", timeout=30)
        assert current["model"] == "zai/glm-4.7"  # medium, not echo/dev
        reroutes = _notes_with(current, '"replaced"')
        assert reroutes, "expected a reroute note naming old and new"
        assert reroutes[0]["replaced"] == "ollama/qwen2.5-coder"
        assert reroutes[0]["routed"] == "zai/glm-4.7"
        assert reroutes[0]["reason"] == "unknown model"
        assert reroutes[0]["skill"] == "medium"  # the story-skill fallback
        assert gateway.calls[-1]["model"] == "zai/glm-4.7"

def wait_for_status(client, story_id: str, want: str, timeout: float = 30.0) -> dict:
    import time

    deadline = time.time() + timeout
    story = None
    while time.time() < deadline:
        story = client.get(f"/api/v1/stories/{story_id}").json()
        if story["status"] == want:
            return story
        time.sleep(0.05)
    raise AssertionError(
        f"story never reached {want}; last: {story and story['status']}, "
        f"error: {story and story.get('error')}"
    )


def test_default_model_prefers_keyed_provider_over_echo(client):
    registry = client.app.state.registry
    # nothing keyed: echo is the honest default
    assert registry.default_model() == "echo/dev"

    # adding a key makes that provider's medium model the default
    client.post("/api/v1/providers/zai/key", json={"key": "zai-key"})
    assert registry.default_model() == "zai/glm-4.7"

    # and /models reflects it for the chat picker
    assert client.get("/api/v1/models").json()["default_model"] == "zai/glm-4.7"
