"""DB-backed provider registry: the catalog is rows, not a file.

Everything the settings page edits lives here: providers, their models, keys,
and skill/cost tiers. Resolution and skill routing are the two read paths the
rest of the app uses.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

from sqlmodel import Session, select

from .db import session_scope
from .models import Provider, ProviderModel
from .presets import ECHO_PROVIDER, PRESETS

log = logging.getLogger(__name__)

TIERS = ("low", "medium", "high")
_TIER_RANK = {"low": 0, "medium": 1, "high": 2}

# -- endpoint reachability -------------------------------------------------
#
# `configured()` says a row has what it needs on paper; a credential-less
# local endpoint (ollama, LM Studio, ...) is only *usable* when something
# actually answers. Routing a story at a dead endpoint just moves the
# failure into the coder run as a litellm "Missing credentials" error.
#
# Both outcomes are cached for PROBE_CACHE_TTL seconds so the scheduler's
# tick loop never re-probes a dead URL more than once per window; the cost
# of negative caching is that a revived local endpoint is noticed <=60s late.
PROBE_TIMEOUT = 1.0
PROBE_CACHE_TTL = 60.0
_PROBE_CACHE: dict[str, tuple[float, bool]] = {}


def _endpoint_reachable(base_url: str) -> bool:
    """True when `base_url` answers within PROBE_TIMEOUT seconds.

    Any HTTP response counts — 404/401 included — because the question is
    "is a server there", not "is this path right". Cached (both outcomes)
    keyed by the URL itself, so per-test base_urls never poison each other.
    """
    now = time.monotonic()
    cached = _PROBE_CACHE.get(base_url)
    if cached is not None and now - cached[0] < PROBE_CACHE_TTL:
        return cached[1]
    reachable = False
    try:
        urllib.request.urlopen(base_url, timeout=PROBE_TIMEOUT)  # noqa: S310
        reachable = True
    except urllib.error.HTTPError:
        reachable = True  # the server answered; the path is simply wrong
    except (urllib.error.URLError, OSError, ValueError):
        reachable = False  # refused / timed out / not a URL we can probe
    _PROBE_CACHE[base_url] = (now, reachable)
    return reachable


class UnknownModelError(KeyError):
    """A model id that is not in the registry."""


# The zone a provider states usage-limit reset times in. z.ai says UTC+8
# (its 26 Oct 429 said "reset at 2026-10-26 19:29:59" = 11:29:59Z), so zai*
# rows default to Asia/Shanghai; every other provider defaults to UTC.
LIMIT_RESET_DEFAULT_ZONES = {"zai": "Asia/Shanghai"}


def limit_reset_zone(provider: Provider) -> str:
    """The provider's IANA zone for naive usage-limit reset timestamps.

    Explicit `limit_reset_tz` on the row wins; else the zai* default; else
    UTC. The headless stream parser reads stated reset times in this zone —
    read as UTC, a z.ai reset would hold the provider 8 hours too long.
    """
    configured = (provider.limit_reset_tz or "").strip()
    if configured:
        return configured
    for prefix, zone in LIMIT_RESET_DEFAULT_ZONES.items():
        if (provider.id or "").startswith(prefix):
            return zone
    return "UTC"


class ResolvedModel:
    __slots__ = ("provider", "model_id", "skill", "cost")

    def __init__(self, provider: Provider, model: ProviderModel):
        self.provider = provider
        self.model_id = model.model_id
        self.skill = model.skill
        self.cost = model.cost

    @property
    def canonical(self) -> str:
        return f"{self.provider.id}/{self.model_id}"


class ProviderRegistry:
    def __init__(self, engine) -> None:
        self.engine = engine

    # -- seeding -------------------------------------------------------

    def seed_if_empty(self) -> None:
        with session_scope(self.engine) as db:
            if db.exec(select(Provider).limit(1)).first() is not None:
                return
            for pid, preset in PRESETS.items():
                self._create_provider_row(db, pid, preset)
            self._create_provider_row(
                db,
                ECHO_PROVIDER["id"],
                {k: v for k, v in ECHO_PROVIDER.items() if k != "id"},
            )

    def _create_provider_row(self, db: Session, pid: str, spec: dict) -> None:
        provider = Provider(
            id=pid,
            display_name=spec["display_name"],
            kind=spec["kind"],
            litellm_prefix=spec.get("litellm_prefix"),
            base_url=spec.get("base_url"),
            env_key=spec.get("env_key"),
            protected=spec.get("protected", False),
        )
        db.add(provider)
        for model in spec.get("models", []):
            db.add(
                ProviderModel(
                    provider_id=pid,
                    model_id=model["model_id"],
                    skill=model.get("skill", "medium"),
                    cost=model.get("cost", "medium"),
                )
            )

    # -- reads -----------------------------------------------------------

    def configured(self, provider: Provider) -> bool:
        if provider.kind == "echo":
            return True
        if provider.api_key:
            return True
        if provider.env_key and os.environ.get(provider.env_key):
            return True
        # keyless local endpoints (e.g. ollama) are usable with a base_url
        return provider.kind == "openai_compatible" and not provider.env_key and bool(
            provider.base_url
        )

    def usable(self, provider: Provider) -> bool:
        """Configured AND actually reachable: routing must only pick models
        whose provider can serve a request right now.

        A stored/env key, the echo provider, or a credential-less
        openai_compatible endpoint whose base_url answers a short-timeout
        probe. Reachability — not a registry row — is the bar for keyless
        endpoints; `configured()` alone would route stories at a local
        ollama that isn't running."""
        if not self.configured(provider):
            return False
        if provider.kind == "echo":
            return True
        if self.effective_key(provider):
            return True
        # credential-less openai_compatible: probe the endpoint
        return bool(provider.base_url) and _endpoint_reachable(provider.base_url)

    def unusable_reason(self, provider: Provider) -> str | None:
        """Why `usable` would skip this provider, or None when it qualifies.

        One short phrase per cause so routing logs and tests can name both
        the model and the reason. Mirrors `usable()` exactly: only a
        credential-less openai_compatible endpoint may qualify via the
        probe — a provider that declares an env_key but has none present is
        "no credentials" no matter what its base_url answers (probing it
        would make routing pick a model the gateway then rejects with
        litellm "Missing credentials")."""
        if provider.kind == "echo":
            return None
        if self.effective_key(provider):
            return None
        keyless_local = provider.kind == "openai_compatible" and not provider.env_key
        if not keyless_local or not provider.base_url:
            return "no credentials"
        if not _endpoint_reachable(provider.base_url):
            return f"endpoint {provider.base_url} unreachable"
        return None

    def effective_key(self, provider: Provider) -> str | None:
        if provider.api_key:
            return provider.api_key
        if provider.env_key:
            return os.environ.get(provider.env_key) or None
        return None

    def resolve(self, canonical: str) -> ResolvedModel:
        provider_id, _, model_id = canonical.partition("/")
        if not model_id:
            raise UnknownModelError(canonical)
        with Session(self.engine) as db:
            provider = db.get(Provider, provider_id)
            if provider is None:
                raise UnknownModelError(canonical)
            model = db.exec(
                select(ProviderModel).where(
                    ProviderModel.provider_id == provider_id,
                    ProviderModel.model_id == model_id,
                )
            ).first()
            if model is None:
                raise UnknownModelError(canonical)
            # refresh provider columns we read after the session closes
            _ = provider.kind, provider.litellm_prefix, provider.base_url
            return ResolvedModel(provider, model)

    def default_model(self) -> str:
        """Default for new chats/stories: the cheapest key-configured real
        provider's medium-skill model; echo only when nothing has a key.

        Explicit configuration (a stored/env key) outranks keyless local
        endpoints — adding a key is the act of choosing a default.
        """
        with Session(self.engine) as db:
            keyed_ids = {
                p.id
                for p in db.exec(select(Provider).where(Provider.enabled)).all()
                if p.kind != "echo"
                and (p.api_key or (p.env_key and os.environ.get(p.env_key)))
            }
            candidates = (
                db.exec(select(ProviderModel).where(ProviderModel.enabled)).all()
                if keyed_ids
                else []
            )
            candidates = [m for m in candidates if m.provider_id in keyed_ids]
            if candidates:
                def sort_key(m: ProviderModel) -> tuple[int, int, str]:
                    skill_penalty = abs(_TIER_RANK.get(m.skill, 1) - 1)
                    return (skill_penalty, _TIER_RANK.get(m.cost, 1), m.model_id)

                best = sorted(candidates, key=sort_key)[0]
                return f"{best.provider_id}/{best.model_id}"
            echo = db.get(Provider, "echo")
            if echo is not None:
                model = db.exec(
                    select(ProviderModel).where(ProviderModel.provider_id == "echo")
                ).first()
                if model is not None:
                    return f"echo/{model.model_id}"
        return "echo/dev"

    def resolve_usable(self, canonical: str) -> tuple[ResolvedModel | None, str | None]:
        """Resolve a canonical model AND say whether it can serve right now.

        Returns `(resolved, None)` when the row exists, its provider is
        enabled, and `unusable_reason()` is None — the caller may run the
        story on it unchanged. Otherwise `(resolved_or_None, reason)`:
        `None` with `"unknown model"` when `resolve()` raises, else the
        resolved row with why it cannot be used ("no credentials",
        "endpoint <url> unreachable"). Lets the scheduler keep a pinned
        model across failures and replace it only when it is genuinely
        unusable — never silently downgrade a story to a cheaper tier.
        """
        try:
            resolved = self.resolve(canonical)
        except UnknownModelError:
            return None, "unknown model"
        provider_id = canonical.partition("/")[0]
        with Session(self.engine) as db:
            provider = db.get(Provider, provider_id)
            if provider is None:
                return None, "unknown model"
            if not provider.enabled:
                return resolved, "provider disabled"
            # refresh provider columns we read after the session closes
            _ = provider.kind, provider.api_key, provider.env_key, provider.base_url
            reason = self.unusable_reason(provider)
        return resolved, reason

    def resolve_for_skill(self, wanted_skill: str) -> ResolvedModel:
        """Cost-aware routing: cheapest usable model whose skill tier meets
        the request; prefer the closest skill tier, then lowest cost.

        Unusable providers (no credentials, or a credential-less endpoint
        that doesn't answer) are skipped with one log line naming the model
        and why, so an operator reading the log can tell a routing decision
        from a dead local endpoint."""
        if wanted_skill not in TIERS:
            raise UnknownModelError(f"skill:{wanted_skill}")
        want = _TIER_RANK[wanted_skill]
        best: tuple[int, int, str, ResolvedModel] | None = None
        skipped: list[str] = []
        with Session(self.engine) as db:
            providers = {
                p.id: p
                for p in db.exec(select(Provider).where(Provider.enabled)).all()
            }
            for model in db.exec(
                select(ProviderModel).where(ProviderModel.enabled)
            ).all():
                provider = providers.get(model.provider_id)
                if provider is None:
                    continue
                rank = _TIER_RANK.get(model.skill)
                if rank is None or rank < want:
                    continue
                reason = self.unusable_reason(provider)
                if reason is not None:
                    skipped.append(f"{provider.id}/{model.model_id} ({reason})")
                    continue
                key = (rank - want, _TIER_RANK.get(model.cost, 1), model.model_id)
                if best is None or key < best[:3]:
                    best = (key[0], key[1], model.model_id, ResolvedModel(provider, model))
        for line in skipped:
            log.warning("routing: skipping %s", line)
        if best is None:
            raise UnknownModelError(f"no usable model for skill {wanted_skill}")
        return best[3]

    def describe(self) -> dict[str, Any]:
        with Session(self.engine) as db:
            providers = db.exec(select(Provider).order_by(Provider.display_name)).all()
            models = db.exec(select(ProviderModel).order_by(ProviderModel.id)).all()
        by_provider: dict[str, list[ProviderModel]] = {}
        for model in models:
            by_provider.setdefault(model.provider_id, []).append(model)
        return {
            "default_model": self.default_model(),
            "providers": [
                {
                    "id": p.id,
                    "display_name": p.display_name,
                    "kind": p.kind,
                    "configured": self.configured(p),
                    "enabled": p.enabled,
                    "protected": p.protected,
                    "base_url": p.base_url,
                    "env_key": p.env_key,
                    "has_key": bool(p.api_key),
                    "key_hint": (p.api_key[-4:] if p.api_key else None),
                    "models": [
                        {
                            "id": f"{p.id}/{m.model_id}",
                            "row_id": m.id,
                            "model_id": m.model_id,
                            "skill": m.skill,
                            "cost": m.cost,
                            "enabled": m.enabled,
                        }
                        for m in by_provider.get(p.id, [])
                    ],
                }
                for p in providers
            ],
        }
