"""Application settings API (get / patch, plus prompt defaults)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..pods import parse_cpu_millicores, parse_memory_bytes
from ..registry import UnknownModelError
from ..settings_store import (
    DEFAULTS,
    get_api_token,
    load_settings,
    prompt_defaults,
    save_settings,
)

router = APIRouter(prefix="/settings", tags=["settings"])

EFFORT_PATTERN = "^(low|medium|high|xhigh|max)?$"
# an image tag (not a full reference): empty, or [A-Za-z0-9_][A-Za-z0-9_.-]{0,127}
WORKER_IMAGE_PATTERN = r"^([A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?$"
# "" is a real value for these (no flag / the story model), so unlike the other
# settings an empty string is kept as "" instead of being normalised to None
CPU_PATTERN = r"^(\d+m|\d+(\.\d+)?)$"
MEMORY_PATTERN = r"^\d+(Mi|Gi)$"
WORKER_RESOURCE_KEYS = (
    "worker_cpu_request",
    "worker_cpu_limit",
    "worker_memory_request",
    "worker_memory_limit",
)
CPU_BOUNDS = (100, 16000)  # millicores
MEMORY_BOUNDS = (256 << 20, 32 << 30)  # bytes
PHASE_MODEL_KEYS = ("spec_model", "review_model")
PHASE_EFFORT_KEYS = ("spec_effort", "code_effort", "review_effort")


class AppSettingsOut(BaseModel):
    default_model: str | None
    model_slots: int
    test_slots: int
    # deprecated alias of model_slots (one release): same value
    max_parallel_stories: int
    story_poll_interval: float
    auto_approve_after_ai_review: bool
    chat_system_prompt: str | None
    project_root: str | None
    chat_tools_enabled: bool
    spec_tools_enabled: bool
    tools_max_calls: int
    tools_timeout_seconds: float
    tools_deny_globs: str
    write_tools_enabled: bool
    permission_mode: str
    allowed_paths: str
    allowed_commands: str
    context_files: str
    verify_command: str
    verify_fix_rounds: int
    verify_timeout: float
    verify_drop_threshold_pct: float
    # bound on the spec -> ai_review reject loop: after this many consecutive
    # AI-review rejections the story parks at human_review instead of
    # re-queueing for another round
    spec_reject_limit: int
    max_provider_retries: int
    self_update_enabled: bool
    spec_writer_prompt: str | None
    ai_reviewer_prompt: str | None
    max_external_wip_per_project: int
    api_auth_enabled: bool
    headless_timeout_seconds: float
    headless_test_timeout_seconds: float
    run_executor: str
    worker_image: str
    worker_fallback_parallel: int
    worker_cpu_request: str
    worker_cpu_limit: str
    worker_memory_request: str
    worker_memory_limit: str
    # Claude Max per-phase model and --effort; "" = story model / no flag
    spec_model: str
    review_model: str
    spec_effort: str
    code_effort: str
    review_effort: str
    review_max_tool_calls: int
    review_session_timeout_seconds: int
    review_test_budget_seconds: int
    # last 4 chars of the API token; None when no token is configured.
    # The full token is returned exactly once, by POST /auth/token.
    api_token_hint: str | None


class AppSettingsUpdate(BaseModel):
    default_model: str | None = Field(default=None)
    model_slots: int | None = Field(
        default=None, ge=1, le=16, description="max LLM calls generating at once"
    )
    test_slots: int | None = Field(
        default=None, ge=1, le=16, description="max verify runs at once"
    )
    max_parallel_stories: int | None = Field(
        default=None, ge=1, le=16, description="deprecated alias of model_slots"
    )
    story_poll_interval: float | None = Field(default=None, ge=0.05, le=60)
    auto_approve_after_ai_review: bool | None = None
    chat_system_prompt: str | None = None
    project_root: str | None = None
    chat_tools_enabled: bool | None = None
    spec_tools_enabled: bool | None = None
    tools_max_calls: int | None = Field(default=None, ge=1, le=160)
    tools_timeout_seconds: float | None = Field(default=None, ge=5, le=3600)
    tools_deny_globs: str | None = None
    write_tools_enabled: bool | None = None
    permission_mode: str | None = Field(
        default=None, description="ask | skip | strict", pattern="^(ask|skip|strict)$"
    )
    allowed_paths: str | None = None
    allowed_commands: str | None = None
    context_files: str | None = None
    verify_command: str | None = Field(
        default=None, description="run in the worktree after the coder; empty = skip"
    )
    verify_fix_rounds: int | None = Field(default=None, ge=0, le=5)
    verify_timeout: float | None = Field(default=None, ge=10, le=3600)
    verify_drop_threshold_pct: float | None = Field(default=None, ge=0, le=100)
    spec_reject_limit: int | None = Field(
        default=None,
        ge=0,
        le=10,
        description=(
            "max consecutive AI-review rejections before the story parks at "
            "human_review; 0 = never park (old unbounded behaviour)"
        ),
    )
    max_provider_retries: int | None = Field(
        default=None,
        ge=1,
        le=20,
        description="attempts before a transient provider error fails the story",
    )
    self_update_enabled: bool | None = None
    spec_writer_prompt: str | None = None
    ai_reviewer_prompt: str | None = None
    max_external_wip_per_project: int | None = Field(default=None, ge=1, le=16)
    api_auth_enabled: bool | None = None
    headless_timeout_seconds: float | None = Field(
        default=None,
        ge=30,
        le=7200,
        description="wall-clock bound for one headless Claude Code child process",
    )
    headless_test_timeout_seconds: float | None = Field(
        default=None,
        ge=10,
        le=900,
        description="cut for a targeted test run inside a headless session",
    )
    run_executor: Literal["in_process", "verify", "pod"] | None = None
    worker_image: str | None = Field(
        default=None,
        description="tag of the fubala-worker image; empty = no worker pods",
        pattern=WORKER_IMAGE_PATTERN,
    )
    worker_fallback_parallel: int | None = Field(
        default=None,
        ge=0,
        le=16,
        description=(
            "max coder runs on the backend node at once when no dev node is free; "
            "0 = never; stories wait for the dev node"
        ),
    )
    worker_cpu_request: str | None = Field(
        default=None, description="worker pod CPU request, e.g. 1500m", pattern=CPU_PATTERN
    )
    worker_cpu_limit: str | None = Field(
        default=None, description="worker pod CPU limit, e.g. 4", pattern=CPU_PATTERN
    )
    worker_memory_request: str | None = Field(
        default=None, description="worker pod memory request, e.g. 2Gi", pattern=MEMORY_PATTERN
    )
    worker_memory_limit: str | None = Field(
        default=None, description="worker pod memory limit, e.g. 6Gi", pattern=MEMORY_PATTERN
    )
    spec_model: str | None = Field(
        default=None, description="provider/model for the spec phase; empty = the story model"
    )
    review_model: str | None = Field(
        default=None,
        description="provider/model for AI review and code review; empty = the story model",
    )
    spec_effort: str | None = Field(
        default=None, description="claude_code --effort; empty = no flag", pattern=EFFORT_PATTERN
    )
    code_effort: str | None = Field(
        default=None, description="claude_code --effort; empty = no flag", pattern=EFFORT_PATTERN
    )
    review_effort: str | None = Field(
        default=None, description="claude_code --effort; empty = no flag", pattern=EFFORT_PATTERN
    )
    review_max_tool_calls: int | None = Field(
        default=None, ge=5, le=200, description="code review: turn / tool-call cap per session"
    )
    review_session_timeout_seconds: int | None = Field(
        default=None, ge=60, le=3600, description="code review: wall clock per session"
    )
    review_test_budget_seconds: int | None = Field(
        default=None,
        ge=60,
        le=3600,
        description="code review: budget for the changed tests and mutations",
    )
    spec_max_output_tokens: int | None = Field(default=None, ge=256, le=131072)
    coder_max_output_tokens: int | None = Field(default=None, ge=256, le=131072)
    agent_max_output_tokens_cap: int | None = Field(default=None, ge=1024, le=131072)


def _settings_out(engine, data: dict) -> AppSettingsOut:
    """Settings + token hint; the full token never reaches this response."""
    token = get_api_token(engine)
    return AppSettingsOut(**data, api_token_hint=token[-4:] if token else None)


@router.get("", response_model=AppSettingsOut)
def get_settings(request: Request) -> AppSettingsOut:
    return _settings_out(
        request.app.state.engine, load_settings(request.app.state.engine)
    )


@router.patch("", response_model=AppSettingsOut)
def patch_settings(payload: AppSettingsUpdate, request: Request) -> AppSettingsOut:
    if payload.default_model:
        try:
            request.app.state.registry.resolve(payload.default_model)
        except UnknownModelError:
            raise HTTPException(
                status_code=400, detail=f"unknown model: {payload.default_model}"
            ) from None
    updates = payload.model_dump(exclude_unset=True)
    for key in PHASE_MODEL_KEYS:
        value = (updates.get(key) or "").strip()
        if value:
            try:
                request.app.state.registry.resolve(value)
            except UnknownModelError:
                raise HTTPException(
                    status_code=400, detail=f"{key}: unknown model: {value}"
                ) from None
    # explicit null clears an override back to default
    updates = {
        k: (
            v.strip() or None
            if k.endswith("_prompt") or k in ("default_model", "project_root")
            else v
        )
        for k, v in updates.items()
    }
    # deprecated alias: a write to max_parallel_stories writes model_slots
    alias = updates.pop("max_parallel_stories", None)
    if alias is not None:
        updates.setdefault("model_slots", alias)
    for key in ("model_slots", "test_slots"):
        if key in updates and updates[key] is None:
            updates[key] = DEFAULTS[key]
    if "run_executor" in updates and updates["run_executor"] is None:
        updates["run_executor"] = DEFAULTS["run_executor"]
    if "worker_fallback_parallel" in updates and updates["worker_fallback_parallel"] is None:
        updates["worker_fallback_parallel"] = DEFAULTS["worker_fallback_parallel"]
    if "worker_fallback_parallel" in updates or "run_executor" in updates:
        merged = {**load_settings(request.app.state.engine), **updates}
        if merged.get("worker_fallback_parallel") == 0 and merged.get("run_executor") != "pod":
            raise HTTPException(
                400,
                "worker_fallback_parallel=0 needs run_executor=pod: otherwise the coder "
                "always runs in the API process and every story would wait forever",
            )
    for key in (
        "review_max_tool_calls",
        "review_session_timeout_seconds",
        "review_test_budget_seconds",
    ):
        if key in updates and updates[key] is None:
            updates[key] = DEFAULTS[key]
    if "worker_image" in updates:
        updates["worker_image"] = (updates["worker_image"] or "").strip()
    for key in (*PHASE_MODEL_KEYS, *PHASE_EFFORT_KEYS):
        if key in updates:
            # "" stays "" (story model / no flag); null resets to the default
            value = updates[key]
            updates[key] = DEFAULTS[key] if value is None else value.strip()
    if any(k in updates for k in WORKER_RESOURCE_KEYS):
        for key in WORKER_RESOURCE_KEYS:
            if key in updates and updates[key] is None:
                updates[key] = DEFAULTS[key]
        _check_worker_resources({**load_settings(request.app.state.engine), **updates})
    return _settings_out(request.app.state.engine, save_settings(request.app.state.engine, updates))


def _check_worker_resources(merged: dict) -> None:
    """Validate the merged request/limit pairs, so a one-sided patch is checked too."""
    for kind, parse, (lo, hi), unit in (
        ("cpu", parse_cpu_millicores, CPU_BOUNDS, "100m-16"),
        ("memory", parse_memory_bytes, MEMORY_BOUNDS, "256Mi-32Gi"),
    ):
        values = {}
        for side in ("request", "limit"):
            key = f"worker_{kind}_{side}"
            try:
                values[side] = parse(merged[key])
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"{key}: {e}") from None
            if not lo <= values[side] <= hi:
                raise HTTPException(
                    status_code=400, detail=f"{key}: {merged[key]} is outside {unit}"
                )
        if values["request"] > values["limit"]:
            raise HTTPException(
                status_code=400,
                detail=f"worker_{kind}_request must not exceed worker_{kind}_limit",
            )


@router.get("/prompt_defaults")
def get_prompt_defaults() -> dict[str, str]:
    return prompt_defaults()
