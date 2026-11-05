"""Live model-list sync: parsing, tier inference, and merge semantics."""

import pytest
from sqlmodel import Session, select

from app.model_sync import SyncError, infer_tiers, sync_provider_models
from app.models import Provider, ProviderModel


def make_provider(db, **kw) -> Provider:
    provider = Provider(
        id=kw.pop("id", "testprov"),
        display_name=kw.pop("display_name", "Test"),
        kind=kw.pop("kind", "openai_compatible"),
        base_url=kw.pop("base_url", "https://api.test/v1"),
        **kw,
    )
    db.add(provider)
    db.add(ProviderModel(provider_id=provider.id, model_id="old-model", skill="high", cost="low"))
    db.commit()
    return provider


def test_infer_tiers_from_names_and_pricing():
    assert infer_tiers("glm-4.9-flash", None) == ("low", "low")
    assert infer_tiers("claude-opus-5", None) == ("high", "high")
    assert infer_tiers("glm-5", None) == ("medium", "medium")
    assert infer_tiers("qwen-max", None) == ("high", "high")
    # real pricing overrides name heuristics for cost
    assert infer_tiers("fancy-pro", 0.2) == ("high", "low")
    assert infer_tiers("tiny-flash", 20.0) == ("low", "high")


def test_sync_merges_and_keeps_manual_tiers(client, monkeypatch):
    from app import model_sync

    payload = {
        "data": [
            {"id": "glm-5"},
            {"id": "glm-4.9-flash"},
            {"id": "old-model"},  # already present: tiers must NOT change
            {"id": "text-embedding-3"},  # filtered out
        ]
    }
    monkeypatch.setattr(model_sync, "_http_get_json", lambda *a, **k: payload)

    engine = client.app.state.engine
    with Session(engine) as db:
        provider = make_provider(db)
        summary = sync_provider_models(db, provider, key="k")
        db.commit()

        assert summary == {"added": 2, "removed": 0, "seen": 3}
        models = {
            m.model_id: m
            for m in db.exec(select(ProviderModel)).all()
            if m.provider_id == provider.id
        }
        assert set(models) == {"old-model", "glm-5", "glm-4.9-flash"}
        assert models["old-model"].skill == "high" and models["old-model"].cost == "low"
        assert models["glm-5"].skill == "medium"
        assert models["glm-4.9-flash"].skill == "low"


def test_sync_openrouter_pricing_and_cap(client, monkeypatch):
    from app import model_sync

    payload = {
        "data": [
            {"id": "anthropic/claude-opus-x", "pricing": {"prompt": "0.000003"}},  # $3/Mtok
            {"id": "z-ai/glm-flash", "pricing": {"prompt": "0.0000001"}},  # $0.1/Mtok
            {"id": "big/opus-max", "pricing": {"prompt": "0.00002"}},  # $20/Mtok
            {"id": "weird/unknown-model", "pricing": {"prompt": "0.000001"}},
        ]
    }
    monkeypatch.setattr(model_sync, "_http_get_json", lambda *a, **k: payload)

    with Session(client.app.state.engine) as db:
        provider = make_provider(
            db, id="or", kind="litellm", litellm_prefix="openrouter", base_url=None
        )
        summary = sync_provider_models(db, provider, key="k")
        db.commit()
        assert summary["added"] == 4
        models = {
            m.model_id: (m.skill, m.cost)
            for m in db.exec(select(ProviderModel)).all()
            if m.provider_id == "or"
        }
        assert models["anthropic/claude-opus-x"] == ("high", "medium")  # $3 -> medium
        assert models["z-ai/glm-flash"] == ("low", "low")  # $0.1 -> low
        assert models["big/opus-max"] == ("high", "high")  # $20 -> high


def test_sync_remove_missing(client, monkeypatch):
    from app import model_sync

    monkeypatch.setattr(
        model_sync, "_http_get_json", lambda *a, **k: {"data": [{"id": "new-only"}]}
    )

    with Session(client.app.state.engine) as db:
        provider = make_provider(db)
        summary = sync_provider_models(db, provider, key="k", remove_missing=True)
        db.commit()
        assert summary["removed"] == 1
        remaining = [
            m.model_id
            for m in db.exec(select(ProviderModel)).all()
            if m.provider_id == provider.id
        ]
        assert remaining == ["new-only"]


def test_sync_requires_key(client, monkeypatch):
    with Session(client.app.state.engine) as db:
        provider = make_provider(
            db, id="ap", kind="litellm", litellm_prefix="anthropic", base_url=None
        )
        with pytest.raises(SyncError, match="key"):
            sync_provider_models(db, provider, key=None)


def test_sync_endpoint_via_api(client, monkeypatch):
    from app import model_sync

    monkeypatch.setattr(
        model_sync, "_http_get_json", lambda *a, **k: {"data": [{"id": "fresh-model"}]}
    )
    client.post("/api/v1/providers", json={"preset": "zai", "id": "zai-live"})
    client.post("/api/v1/providers/zai-live/key", json={"key": "k"})
    res = client.post("/api/v1/providers/zai-live/refresh_models", json={})
    assert res.status_code == 200
    body = res.json()
    assert body["sync"]["added"] >= 1
    assert any(m["model_id"] == "fresh-model" for m in body["models"])
