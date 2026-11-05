"""Provider CRUD: the settings page's backend.

Providers are database rows created from presets (or from scratch) — after
creation everything is editable: name, base URL, key, models, skill/cost
tiers, enabled. The registry is the only other code that reads these rows.
"""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ..db import session_scope
from ..models import Provider, ProviderModel, utcnow
from ..presets import PRESETS
from ..registry import TIERS

router = APIRouter(prefix="/providers", tags=["providers"])

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class ProviderCreate(BaseModel):
    preset: str = Field(description="One of the preset ids; prefills the row")
    id: str | None = Field(default=None, description="Slug override; defaults to the preset id")
    display_name: str | None = None
    base_url: str | None = None
    env_key: str | None = None
    api_key: str | None = None


class ProviderUpdate(BaseModel):
    display_name: str | None = None
    base_url: str | None = None
    env_key: str | None = None
    litellm_prefix: str | None = None
    kind: str | None = Field(
        default=None, description="Protocol: litellm | openai_compatible | echo"
    )
    enabled: bool | None = None
    limit_reset_tz: str | None = Field(
        default=None,
        description="IANA zone the provider states usage-limit reset times in",
    )


class KeyBody(BaseModel):
    key: str = Field(description="API key; empty string clears the stored key")


class ModelCreate(BaseModel):
    model_id: str = Field(min_length=1, max_length=200)
    skill: str = "medium"
    cost: str = "medium"


class ModelUpdate(BaseModel):
    skill: str | None = None
    cost: str | None = None
    enabled: bool | None = None


def _check_tier(value: str | None, field: str) -> None:
    if value is not None and value not in TIERS:
        raise HTTPException(status_code=400, detail=f"{field} must be one of {TIERS}")


def _get_provider(db: Session, provider_id: str) -> Provider:
    provider = db.get(Provider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="provider not found")
    return provider


def _provider_out(db: Session, provider: Provider, registry) -> dict:
    models = db.exec(
        select(ProviderModel)
        .where(ProviderModel.provider_id == provider.id)
        .order_by(ProviderModel.id)
    ).all()
    return {
        "id": provider.id,
        "display_name": provider.display_name,
        "kind": provider.kind,
        "litellm_prefix": provider.litellm_prefix,
        "base_url": provider.base_url,
        "env_key": provider.env_key,
        "has_key": bool(provider.api_key),
        "key_hint": provider.api_key[-4:] if provider.api_key else None,
        "protected": provider.protected,
        "enabled": provider.enabled,
        "limit_reset_tz": provider.limit_reset_tz,
        "configured": registry.configured(provider),
        "models": [
            {
                "id": f"{provider.id}/{m.model_id}",
                "row_id": m.id,
                "model_id": m.model_id,
                "skill": m.skill,
                "cost": m.cost,
                "enabled": m.enabled,
            }
            for m in models
        ],
    }


def _presets_out() -> list[dict]:
    return [
        {
            "id": pid,
            "display_name": spec["display_name"],
            "kind": spec["kind"],
            "base_url": spec["base_url"],
            "env_key": spec["env_key"],
            "models": [m["model_id"] for m in spec["models"]],
        }
        for pid, spec in PRESETS.items()
    ]


@router.get("/presets")
def list_presets() -> list[dict]:
    """For the '+' add-provider dropdown."""
    return _presets_out()


@router.get("")
def list_providers(request: Request) -> list[dict]:
    with session_scope(request.app.state.engine) as db:
        providers = db.exec(select(Provider).order_by(Provider.display_name)).all()
        return [_provider_out(db, p, request.app.state.registry) for p in providers]


@router.get("/limits")
def list_provider_limits(request: Request) -> list[dict]:
    """Active provider usage-limit holds: a headless 429/usage-limit answer
    holds the provider until the stated reset time. In-memory state — an
    empty list right after a restart is normal (the next run re-detects)."""
    runner = getattr(request.app.state, "story_runner", None)
    if runner is None:
        return []
    return runner.provider_holds()


@router.post("/{provider_id}/limits/clear", status_code=status.HTTP_204_NO_CONTENT)
def clear_provider_limit(provider_id: str, request: Request) -> Response:
    """Operator escape hatch: forget the provider's usage-limit hold so
    headless pickup resumes at the next scheduler tick."""
    runner = getattr(request.app.state, "story_runner", None)
    if runner is None or not runner.clear_provider_hold(provider_id):
        raise HTTPException(status_code=404, detail="no active limit hold")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_provider(payload: ProviderCreate, request: Request) -> dict:
    preset = PRESETS.get(payload.preset)
    if preset is None:
        raise HTTPException(status_code=400, detail=f"unknown preset: {payload.preset}")
    provider_id = (payload.id or payload.preset).strip().lower()
    if not _SLUG.match(provider_id):
        raise HTTPException(status_code=400, detail="id must be a lowercase slug (a-z, 0-9, -)")
    with session_scope(request.app.state.engine) as db:
        if db.get(Provider, provider_id) is not None:
            raise HTTPException(
                status_code=409, detail=f"provider id already exists: {provider_id}"
            )
        provider = Provider(
            id=provider_id,
            display_name=payload.display_name or preset["display_name"],
            kind=preset["kind"],
            litellm_prefix=preset["litellm_prefix"],
            base_url=payload.base_url
            if payload.base_url is not None
            else preset["base_url"],
            env_key=payload.env_key if payload.env_key is not None else preset["env_key"],
            api_key=payload.api_key or None,
        )
        db.add(provider)
        for model in preset["models"]:
            db.add(
                ProviderModel(
                    provider_id=provider_id,
                    model_id=model["model_id"],
                    skill=model.get("skill", "medium"),
                    cost=model.get("cost", "medium"),
                )
            )
        db.flush()
        return _provider_out(db, provider, request.app.state.registry)


@router.patch("/{provider_id}")
def update_provider(provider_id: str, payload: ProviderUpdate, request: Request) -> dict:
    with session_scope(request.app.state.engine) as db:
        provider = _get_provider(db, provider_id)
        if payload.kind is not None:
            if payload.kind not in ("litellm", "openai_compatible", "claude_code", "echo"):
                raise HTTPException(
                    status_code=400, detail="kind must be litellm|openai_compatible|claude_code"
                )
            provider.kind = payload.kind
        for field in ("display_name", "base_url", "env_key", "litellm_prefix"):
            value = getattr(payload, field)
            if value is not None:
                setattr(provider, field, value)
        if payload.limit_reset_tz is not None:
            zone = payload.limit_reset_tz.strip()
            if zone:
                try:
                    ZoneInfo(zone)
                except (ZoneInfoNotFoundError, ValueError) as e:
                    raise HTTPException(
                        status_code=400, detail=f"unknown IANA time zone: {zone}"
                    ) from e
                provider.limit_reset_tz = zone
            else:
                provider.limit_reset_tz = None  # back to the zone default
        if payload.enabled is not None:
            provider.enabled = payload.enabled
        provider.updated_at = utcnow()
        db.add(provider)
        db.flush()
        return _provider_out(db, provider, request.app.state.registry)


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider(provider_id: str, request: Request) -> Response:
    with session_scope(request.app.state.engine) as db:
        provider = _get_provider(db, provider_id)
        if provider.protected:
            raise HTTPException(status_code=409, detail="protected provider cannot be deleted")
        for model in db.exec(
            select(ProviderModel).where(ProviderModel.provider_id == provider_id)
        ).all():
            db.delete(model)
        db.delete(provider)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{provider_id}/key")
def set_key(provider_id: str, payload: KeyBody, request: Request) -> dict:
    with session_scope(request.app.state.engine) as db:
        provider = _get_provider(db, provider_id)
        provider.api_key = payload.key.strip() or None
        provider.updated_at = utcnow()
        db.add(provider)
        db.flush()
        return _provider_out(db, provider, request.app.state.registry)


@router.post("/{provider_id}/models", status_code=status.HTTP_201_CREATED)
def add_model(provider_id: str, payload: ModelCreate, request: Request) -> dict:
    _check_tier(payload.skill, "skill")
    _check_tier(payload.cost, "cost")
    with session_scope(request.app.state.engine) as db:
        provider = _get_provider(db, provider_id)
        existing = db.exec(
            select(ProviderModel).where(
                ProviderModel.provider_id == provider_id,
                ProviderModel.model_id == payload.model_id,
            )
        ).first()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"model already exists: {payload.model_id}")
        db.add(
            ProviderModel(
                provider_id=provider_id,
                model_id=payload.model_id,
                skill=payload.skill,
                cost=payload.cost,
            )
        )
        db.flush()
        return _provider_out(db, provider, request.app.state.registry)


@router.patch("/{provider_id}/models/{model_row_id}")
def update_model(
    provider_id: str, model_row_id: int, payload: ModelUpdate, request: Request
) -> dict:
    _check_tier(payload.skill, "skill")
    _check_tier(payload.cost, "cost")
    with session_scope(request.app.state.engine) as db:
        _get_provider(db, provider_id)
        model = db.get(ProviderModel, model_row_id)
        if model is None or model.provider_id != provider_id:
            raise HTTPException(status_code=404, detail="model not found")
        for field in ("skill", "cost", "enabled"):
            value = getattr(payload, field)
            if value is not None:
                setattr(model, field, value)
        db.add(model)
        db.flush()
        return _provider_out(db, db.get(Provider, provider_id), request.app.state.registry)


@router.delete("/{provider_id}/models/{model_row_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model(provider_id: str, model_row_id: int, request: Request) -> Response:
    with session_scope(request.app.state.engine) as db:
        _get_provider(db, provider_id)
        model = db.get(ProviderModel, model_row_id)
        if model is None or model.provider_id != provider_id:
            raise HTTPException(status_code=404, detail="model not found")
        db.delete(model)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class RefreshBody(BaseModel):
    remove_missing: bool = False


@router.post("/{provider_id}/refresh_models")
def refresh_models(
    provider_id: str, payload: RefreshBody | None, request: Request
) -> dict:
    """Pull the live model list from the provider and merge it in.

    Existing models keep their tiers; new ones get inferred skill/cost.
    Set remove_missing to drop rows the provider no longer lists.
    """
    from ..model_sync import SyncError, sync_provider_models

    with session_scope(request.app.state.engine) as db:
        provider = _get_provider(db, provider_id)
        key = request.app.state.registry.effective_key(provider)
        try:
            summary = sync_provider_models(
                db, provider, key, remove_missing=(payload.remove_missing if payload else False)
            )
        except SyncError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        provider.updated_at = utcnow()
        db.add(provider)
        db.flush()
        out = _provider_out(db, provider, request.app.state.registry)
        out["sync"] = summary
        return out


@router.get("/{provider_id}/remote_models")
def remote_models(provider_id: str, request: Request) -> list[dict]:
    """The provider's live model list with inferred tiers — read-only preview
    for the searchable add-model combobox (nothing is saved)."""
    from ..model_sync import SyncError, fetch_remote_models, filter_and_rank, infer_tiers

    with session_scope(request.app.state.engine) as db:
        provider = _get_provider(db, provider_id)
        key = request.app.state.registry.effective_key(provider)
        try:
            remote = filter_and_rank(fetch_remote_models(provider, key), cap=200)
        except SyncError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return [
            {
                "model_id": m.model_id,
                "skill": tiers[0],
                "cost": tiers[1],
            }
            for m in remote
            for tiers in [infer_tiers(m.model_id, m.cost_usd_mtok)]
        ]
