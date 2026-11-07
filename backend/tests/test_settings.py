"""Application settings: storage, validation, and live effects."""

import pytest
from conftest import MINIMAL_SPEC, PRODUCT_DEFAULTS, FakeGateway


def test_settings_roundtrip_and_defaults(client):
    body = client.get("/api/v1/settings").json()
    assert body["model_slots"] == 2
    assert body["test_slots"] == 2
    assert body["max_parallel_stories"] == 2  # deprecated alias of model_slots
    assert body["auto_approve_after_ai_review"] is False
    assert body["default_model"] is None

    body = client.patch(
        "/api/v1/settings",
        json={"model_slots": 4, "test_slots": 1, "chat_system_prompt": "You are blunt."},
    ).json()
    assert body["model_slots"] == 4
    assert body["test_slots"] == 1
    assert body["max_parallel_stories"] == 4
    assert body["chat_system_prompt"] == "You are blunt."

    # invalid model rejected; prompt cleared with empty string
    assert (
        client.patch("/api/v1/settings", json={"default_model": "nope/x"}).status_code == 400
    )
    body = client.patch("/api/v1/settings", json={"chat_system_prompt": ""}).json()
    assert body["chat_system_prompt"] is None


def test_chat_system_prompt_reaches_gateway(client, gateway):
    client.patch("/api/v1/settings", json={"chat_system_prompt": "Answer in haiku."})
    sid = client.post("/api/v1/chat/sessions", json={}).json()["id"]
    client.post(f"/api/v1/chat/sessions/{sid}/messages", json={"content": "hi"})
    assert gateway.calls[-1]["messages"][0] == {"role": "system", "content": "Answer in haiku."}


def test_prompt_defaults_endpoint(client):
    defaults = client.get("/api/v1/settings/prompt_defaults").json()
    assert "spec writer" in defaults["spec_writer_prompt"].lower()
    assert "VERDICT" in defaults["ai_reviewer_prompt"]


def test_prompt_override_reaches_runner(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "prompt.db"))
    gateway = FakeGateway()
    with TestClient(create_app(gateway=gateway)) as client:
        client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
        client.patch(
            "/api/v1/settings", json={"spec_writer_prompt": "CUSTOM PROMPT OVERRIDE"}
        )
        story = client.post("/api/v1/stories", json={"title": "x"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")

        import time

        deadline = time.time() + 30
        while time.time() < deadline:
            if client.get(f"/api/v1/stories/{story['id']}").json()["status"] in (
                "human_review",
                "failed",
            ):
                break
            time.sleep(0.05)
        assert gateway.calls[0]["messages"][0]["content"] == "CUSTOM PROMPT OVERRIDE"


def test_auto_approve_skips_human_gate(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_stories import SequencedGateway

    from app.main import create_app

    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "autoapprove.db"))
    # auto-approve must require an EXPLICIT verdict — unparsable reviews
    # still go to the human gate even when the toggle is on
    gateway = SequencedGateway(
        [[MINIMAL_SPEC], ["looks good\n\nVERDICT: APPROVED"]]
    )
    with TestClient(create_app(gateway=gateway)) as client:
        client.patch("/api/v1/settings", json={"story_poll_interval": 0.05})
        client.patch("/api/v1/settings", json={"auto_approve_after_ai_review": True})
        story = client.post("/api/v1/stories", json={"title": "auto"}).json()
        client.post(f"/api/v1/stories/{story['id']}/queue")

        import time

        deadline = time.time() + 30
        final = None
        while time.time() < deadline:
            final = client.get(f"/api/v1/stories/{story['id']}").json()
            if final["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert final["status"] == "done"
        assert final["approved_by"] == "auto (ai-approved)"


PHASE_KEYS = ("spec_model", "review_model", "spec_effort", "code_effort", "review_effort")


def test_phase_defaults_are_the_agreed_ones():
    assert {k: PRODUCT_DEFAULTS[k] for k in PHASE_KEYS} == {
        "spec_model": "claude-max/claude-opus-5-5",
        "review_model": "",
        "spec_effort": "high",
        "code_effort": "medium",
        "review_effort": "medium",
    }


def test_phase_settings_are_served_and_old_databases_get_the_defaults(client):
    # a stored blob from before these keys existed: the defaults fill in
    from sqlmodel import Session

    from app.models import AppSetting
    from app.settings_store import SETTINGS_KEY

    with Session(client.app.state.engine) as db:
        db.merge(AppSetting(key=SETTINGS_KEY, value='{"max_parallel_stories": 3}'))
        db.commit()
    body = client.get("/api/v1/settings").json()
    # the deprecated key is read as model_slots
    assert body["model_slots"] == 3
    assert body["max_parallel_stories"] == 3
    assert body["test_slots"] == 2
    for key in PHASE_KEYS:
        assert key in body
    assert body["spec_effort"] == "high"
    assert body["code_effort"] == "medium"
    assert body["review_effort"] == "medium"
    assert body["review_model"] == ""


def test_empty_string_is_a_real_value_for_phase_settings(client):
    body = client.patch(
        "/api/v1/settings",
        json={
            "spec_model": "claude-max/claude-opus-5-5",
            "spec_effort": "max",
            "code_effort": "low",
            "review_effort": "xhigh",
        },
    ).json()
    assert body["spec_model"] == "claude-max/claude-opus-5-5"
    assert (body["spec_effort"], body["code_effort"], body["review_effort"]) == (
        "max", "low", "xhigh",
    )
    body = client.patch(
        "/api/v1/settings",
        json={"spec_model": "", "spec_effort": "", "code_effort": "", "review_effort": ""},
    ).json()
    assert body["spec_model"] == ""  # kept as "", not turned into null
    assert body["spec_effort"] == body["code_effort"] == body["review_effort"] == ""
    assert client.get("/api/v1/settings").json()["spec_model"] == ""


def test_null_resets_a_phase_setting_to_its_default(client):
    client.patch("/api/v1/settings", json={"code_effort": "high", "review_model": ""})
    body = client.patch("/api/v1/settings", json={"code_effort": None}).json()
    assert body["code_effort"] == PRODUCT_DEFAULTS["code_effort"]


@pytest.mark.parametrize("key", ["spec_effort", "code_effort", "review_effort"])
@pytest.mark.parametrize("bad", ["ultra", "HIGH", "med"])
def test_unknown_effort_levels_are_rejected(client, key, bad):
    res = client.patch("/api/v1/settings", json={key: bad})
    assert res.status_code == 422
    assert client.get("/api/v1/settings").json()[key] != bad


@pytest.mark.parametrize("key", ["spec_model", "review_model"])
def test_unknown_phase_model_is_rejected_naming_the_setting(client, key):
    res = client.patch("/api/v1/settings", json={key: "nope/not-a-model"})
    assert res.status_code == 400
    assert key in res.json()["detail"]
    assert "nope/not-a-model" in res.json()["detail"]
    assert client.get("/api/v1/settings").json()[key] != "nope/not-a-model"


def test_worker_resource_settings(client):
    url = "/api/v1/settings"
    body = client.get(url).json()
    assert body["worker_cpu_request"] == "1500m"
    assert body["worker_cpu_limit"] == "4"
    assert body["worker_memory_request"] == "2Gi"
    assert body["worker_memory_limit"] == "6Gi"
    ok = client.patch(url, json={"worker_cpu_request": "2", "worker_memory_limit": "8Gi"})
    assert ok.status_code == 200 and ok.json()["worker_cpu_request"] == "2"
    bad = [
        {"worker_cpu_request": "5"},  # above the stored limit of 4 (merged check)
        {"worker_cpu_limit": "1"},  # below the stored request of 2
        {"worker_cpu_request": "1.5m"},  # bad format
        {"worker_memory_request": "2G"},
        {"worker_cpu_request": "50m"},  # out of bounds
        {"worker_cpu_request": "16", "worker_cpu_limit": "17"},
        {"worker_memory_request": "128Mi"},
        {"worker_memory_request": "8Gi", "worker_memory_limit": "40Gi"},
    ]
    for payload in bad:
        r = client.patch(url, json=payload)
        assert r.status_code in (400, 422), payload
        if r.status_code == 400:
            assert "worker_" in r.json()["detail"]
    assert client.get(url).json()["worker_cpu_request"] == "2"
    reset = client.patch(url, json={"worker_cpu_request": None, "worker_memory_limit": None})
    assert reset.status_code == 200
    assert reset.json()["worker_cpu_request"] == "1500m"
    assert reset.json()["worker_memory_limit"] == "6Gi"


def test_slot_settings_bounds_and_the_deprecated_alias(client):
    for key in ("model_slots", "test_slots", "max_parallel_stories"):
        assert client.patch("/api/v1/settings", json={key: 0}).status_code == 422
        assert client.patch("/api/v1/settings", json={key: 17}).status_code == 422
    # a PATCH to the old key writes model_slots
    body = client.patch("/api/v1/settings", json={"max_parallel_stories": 5}).json()
    assert body["model_slots"] == 5 and body["max_parallel_stories"] == 5
    assert client.get("/api/v1/settings").json()["model_slots"] == 5
    # the stored row carries the new key only
    from sqlmodel import Session

    from app.models import AppSetting
    from app.settings_store import SETTINGS_KEY

    with Session(client.app.state.engine) as db:
        stored = db.get(AppSetting, SETTINGS_KEY).value
    assert '"model_slots": 5' in stored and "max_parallel_stories" not in stored
    # the new key wins over the alias in one body; null resets to the default
    body = client.patch(
        "/api/v1/settings", json={"model_slots": 3, "max_parallel_stories": 9}
    ).json()
    assert body["model_slots"] == 3
    body = client.patch("/api/v1/settings", json={"model_slots": None, "test_slots": None}).json()
    assert body["model_slots"] == 2 and body["test_slots"] == 2


def test_worker_fallback_parallel_zero_needs_pod_executor(client):
    assert client.patch("/api/v1/settings", json={"worker_fallback_parallel": 0}).status_code == 400
    r = client.patch(
        "/api/v1/settings", json={"run_executor": "pod", "worker_fallback_parallel": 0}
    )
    assert r.status_code == 200
    assert client.get("/api/v1/settings").json()["worker_fallback_parallel"] == 0
    assert client.patch("/api/v1/settings", json={"run_executor": "verify"}).status_code == 400
    r = client.patch("/api/v1/settings", json={"worker_fallback_parallel": None})
    assert r.status_code == 200
    assert client.get("/api/v1/settings").json()["worker_fallback_parallel"] == 2
