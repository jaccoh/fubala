"""Fetch live model lists from providers and sync them into the registry.

Preset seeds are just a starting point; this module makes the catalog current.
Each provider family exposes an OpenAI-style (or mappable) model-list endpoint;
we fetch it, filter non-chat models, infer skill/cost tiers, and merge —
existing rows keep their (possibly hand-tuned) tiers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlmodel import Session, select

from .models import Provider, ProviderModel

log = logging.getLogger(__name__)

# litellm_prefix -> (models url, auth style)
LITELLM_LIST_URLS = {
    "openai": ("https://api.openai.com/v1/models", "bearer"),
    "anthropic": ("https://api.anthropic.com/v1/models", "anthropic"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/models", "query"),
    "openrouter": ("https://openrouter.ai/api/v1/models", "bearer"),
}

FILTER_SUBSTRINGS = (
    "embed",
    "moderation",
    "whisper",
    "tts",
    "dall-e",
    "imagen",
    "rerank",
    "aqa",
    "veo",
    # image / video / audio / roleplay / code-completion families (z.ai & co)
    "cogview",
    "cogvideo",
    "video",
    "voice",
    "image",
    "characterglm",
    "charglm",
    "codegeex",
    "abab",
    "sora",
)

# families preferred when capping a huge catalog (openrouter)
FAMILIES = (
    "claude", "gpt", "gemini", "glm", "deepseek", "qwen", "llama", "mistral", "kimi", "grok",
)

LOW_MARKERS = ("mini", "flash", "haiku", "nano", "small", "lite", ":free", "-free")
HIGH_MARKERS = ("opus", "pro", "max", "large", "ultra", "thinking", "reasoning", "frontier")

MAX_MODELS_PER_SYNC = 60


class SyncError(Exception):
    """User-presentable failure (missing key, unreachable endpoint, bad payload)."""


@dataclass
class RemoteModel:
    model_id: str
    cost_usd_mtok: float | None = None


def _http_get_json(url: str, headers: dict, params: dict | None = None) -> dict:
    import httpx

    client = httpx.Client(timeout=20)
    try:
        res = client.get(url, headers=headers, params=params)
    except httpx.HTTPError as e:
        raise SyncError(f"could not reach {url}: {e}") from e
    finally:
        client.close()
    if res.status_code in (401, 403):
        raise SyncError(
            f"provider rejected the request (HTTP {res.status_code}) — is the key set?"
        )
    if res.status_code >= 400:
        raise SyncError(f"{url} -> {res.status_code}: {res.text[:200]}")
    return res.json()


def fetch_remote_models(provider: Provider, key: str | None) -> list[RemoteModel]:
    if provider.kind == "openai_compatible":
        if not provider.base_url:
            raise SyncError("provider has no base_url to query")
        url = provider.base_url.rstrip("/") + "/models"
        auth = "bearer"
    elif provider.kind == "litellm":
        spec = LITELLM_LIST_URLS.get(provider.litellm_prefix or "")
        if spec is None:
            raise SyncError(
                f"no model-list endpoint known for prefix '{provider.litellm_prefix}'; "
                "add models manually"
            )
        url, auth = spec
        if provider.base_url:  # custom gateway override (e.g. zai plan)
            base = provider.base_url.rstrip("/")
            if provider.litellm_prefix == "anthropic":
                url, auth = base + "/v1/models", "anthropic"  # x-api-key protocol
            else:
                url, auth = base + "/models", "bearer"
    elif provider.kind == "claude_code":
        raise SyncError("Claude Max has no model-list endpoint; add models manually")
    else:
        raise SyncError("echo providers have nothing to sync")

    headers: dict = {}
    params: dict | None = None
    if auth == "bearer":
        if key:
            headers["Authorization"] = f"Bearer {key}"
        elif provider.kind == "litellm" and provider.litellm_prefix == "openai":
            raise SyncError("provider needs a key to list models")
        # else try keyless: openrouter lists publicly, local gateways usually do
    elif auth == "anthropic":
        if not key:
            raise SyncError("provider needs a key to list models")
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
    elif auth == "query":
        if not key:
            raise SyncError("provider needs a key to list models")
        params = {"key": key}

    payload = _http_get_json(url, headers, params)
    return _parse_models(payload)


def _parse_models(payload: dict) -> list[RemoteModel]:
    out: list[RemoteModel] = []
    if isinstance(payload.get("data"), list):  # OpenAI / Anthropic / OpenRouter shape
        for item in payload["data"]:
            model_id = item.get("id") if isinstance(item, dict) else None
            if not model_id:
                continue
            cost = None
            pricing = item.get("pricing")
            if isinstance(pricing, dict) and pricing.get("prompt"):
                try:
                    cost = float(pricing["prompt"]) * 1_000_000  # $/token -> $/Mtok
                except ValueError:
                    cost = None
            out.append(RemoteModel(model_id=model_id, cost_usd_mtok=cost))
    elif isinstance(payload.get("models"), list):  # Gemini shape
        for item in payload["models"]:
            name = (item.get("name") or "").removeprefix("models/")
            if not name:
                continue
            methods = item.get("supportedGenerationMethods") or []
            if methods and "generateContent" not in methods:
                continue
            out.append(RemoteModel(model_id=name))
    if not out:
        raise SyncError(
            "model list came back empty or unrecognized — "
            f"keys: {list(payload)[:8]}, preview: {str(payload)[:180]}"
        )
    return out


def infer_tiers(model_id: str, cost_usd_mtok: float | None) -> tuple[str, str]:
    lower = model_id.lower()
    if any(m in lower for m in LOW_MARKERS):
        skill = "low"
    elif any(m in lower for m in HIGH_MARKERS):
        skill = "high"
    else:
        skill = "medium"

    if cost_usd_mtok is not None:
        cost = "low" if cost_usd_mtok <= 0.5 else ("medium" if cost_usd_mtok <= 5 else "high")
    elif ":free" in lower or "-free" in lower:
        cost = "low"
    elif skill == "low":
        cost = "low"
    elif skill == "high":
        cost = "high"
    else:
        cost = "medium"
    return skill, cost


def filter_and_rank(remote: list[RemoteModel], cap: int = MAX_MODELS_PER_SYNC) -> list[RemoteModel]:
    remote = [m for m in remote if not any(s in m.model_id.lower() for s in FILTER_SUBSTRINGS)]
    if len(remote) > cap:
        remote.sort(
            key=lambda m: (
                0 if any(f in m.model_id.lower() for f in FAMILIES) else 1,
                m.cost_usd_mtok if m.cost_usd_mtok is not None else 99,
                m.model_id,
            )
        )
        remote = remote[:cap]
    return remote


def sync_provider_models(
    db: Session, provider: Provider, key: str | None, *, remove_missing: bool = False
) -> dict[str, int]:
    """Merge the remote list into the provider's model rows.

    Existing rows keep their tiers (they may be hand-tuned); new models get
    inferred tiers. `remove_missing` drops rows the provider no longer lists.
    """
    remote = filter_and_rank(fetch_remote_models(provider, key))

    existing = db.exec(
        select(ProviderModel).where(ProviderModel.provider_id == provider.id)
    ).all()
    by_model_id = {m.model_id: m for m in existing}

    added = 0
    for remote_model in remote:
        if remote_model.model_id in by_model_id:
            continue
        skill, cost = infer_tiers(remote_model.model_id, remote_model.cost_usd_mtok)
        db.add(
            ProviderModel(
                provider_id=provider.id,
                model_id=remote_model.model_id,
                skill=skill,
                cost=cost,
            )
        )
        added += 1

    removed = 0
    if remove_missing:
        remote_ids = {m.model_id for m in remote}
        for model in existing:
            if model.model_id not in remote_ids:
                db.delete(model)
                removed += 1

    return {"added": added, "removed": removed, "seen": len(remote)}
