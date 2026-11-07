"""Application settings: one JSON row in the DB, editable from the UI.

Env vars (FUBALA_*) remain the bootstrap defaults; anything set here overrides
them at runtime — no restart needed. Prompt overrides fall back to the bundled
markdown files when unset.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session

from .models import AppSetting

SETTINGS_KEY = "app"
# the API token deliberately lives in its OWN row: load_settings() drops keys
# that are not in DEFAULTS, and GET /settings echoes that merged dict — the
# raw token must never ride along in either
API_TOKEN_KEY = "api_token"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

PROMPT_FILES = {
    "spec_writer_prompt": "spec_writer.md",
    "ai_reviewer_prompt": "ai_reviewer.md",
}

DEFAULTS: dict[str, Any] = {
    "default_model": None,  # None = auto (cheapest keyed medium)
    # concurrency is slots, not stories: at most model_slots LLM calls
    # generating and test_slots verify runs at once; any number of stories
    # may be in flight. max_parallel_stories is a deprecated alias of
    # model_slots for one release (DEPRECATED_ALIASES below).
    "model_slots": 2,
    "test_slots": 2,
    "story_poll_interval": 0.5,
    "auto_approve_after_ai_review": False,
    "chat_system_prompt": None,
    "project_root": None,  # enables read-only filesystem tools when set
    "chat_tools_enabled": True,
    "spec_tools_enabled": True,
    "tools_max_calls": 40,
    "tools_timeout_seconds": 600.0,
    "tools_deny_globs": "",  # extra denied filename globs, comma/space separated
    "write_tools_enabled": False,  # write_file/run_command for the coder stage
    # ask: out-of-project paths prompt the operator live (zcode-style)
    # skip: auto-grant everything (yolo; jail + budgets stay)
    # strict: deny out-of-project outright, no prompt
    "permission_mode": "ask",
    "allowed_commands": "",  # command allowlist override; empty = safe defaults
    "allowed_paths": "",  # globally granted extra roots (comma separated)
    # self-improvement: verify runs in the story worktree after the coder
    # commits; failures bounce back to the coder for bounded fix rounds.
    # Empty command = verification skipped (and the merge gate with it).
    "verify_command": "",
    "verify_fix_rounds": 2,
    "verify_timeout": 600.0,
    # a green verify running this many percent fewer tests than the last
    # green run on main (adjusted for the story's own added tests) is flagged
    "verify_drop_threshold_pct": 2.0,
    # bound on the spec -> ai_review reject loop: after this many consecutive
    # AI-review rejections the story parks at human_review instead of
    # re-queueing, so a disagreeing reviewer and writer cannot loop forever
    "spec_reject_limit": 2,
    # transient provider errors (litellm 5xx / connection drops, z.ai code
    # 1234) requeue with backoff; after this many attempts the story fails
    # with the last error and its dependents cascade
    "max_provider_retries": 5,
    # after a merge, restart the server on the new code and health-check it;
    # a server that won't boot gets its merge auto-reverted
    "self_update_enabled": False,
    # harness-injected project instructions (comma separated names/globs):
    # matching files in the project root are prepended to the spec-writer and
    # coder system prompts — the model never has to remember to read them
    "context_files": "AGENTS.md, CLAUDE.md, HANDOVER.md",
    "spec_writer_prompt": None,  # None = bundled prompts/spec_writer.md
    "ai_reviewer_prompt": None,  # None = bundled prompts/ai_reviewer.md
    # per-round output budget for the story phases. A phase that burns its
    # whole budget thinking (finish_reason length/max_tokens, no text) is
    # retried once with the budget doubled, up to the cap below — see
    # app/agent.py. Raise spec_max_output_tokens if specs keep failing with
    # "spec phase produced no spec".
    # None = the gateway's own round budget (Settings.max_output_tokens,
    # FUBALA_MAX_OUTPUT_TOKENS); set a number only to override it per phase.
    "spec_max_output_tokens": None,
    "coder_max_output_tokens": None,
    "agent_max_output_tokens_cap": 32768,
    # external execution: how many claimed external stories may be
    # in_progress at once per scope (project, or global when unprojected)
    "max_external_wip_per_project": 1,
    # require a Bearer token (or loopback/session trust) for /api/v1/* when
    # the board is reachable from beyond this machine. Default off: the local
    # UI keeps working with zero configuration either way.
    "api_auth_enabled": False,
    # wall-clock bound for ONE headless Claude Code child process (the CODE
    # stage of a headless-execution story). Only the headless path reads it;
    # tools_timeout_seconds (the pump's per-tool-call bound) is untouched.
    "headless_timeout_seconds": 2400.0,
    # cut for a targeted test run the headless CLI starts itself (the full
    # suite runs outside the session, see headless.RUN_TESTS_MARKER)
    "headless_test_timeout_seconds": 120.0,
    # worker pods: in_process (default) | verify | pod. verify/pod run every
    # trusted verify/test command in a per-run pod (app/pods.py); pod is
    # identical to verify until the coder itself moves into the pod.
    "run_executor": "in_process",
    # tag of $FUBALA_WORKER_IMAGE_REPO; empty = no pods (in-process)
    "worker_image": "",
    # most coder runs that may load the backend's own node at once (worker
    # pods placed there plus in-process runs); extra runs requeue and wait
    "worker_fallback_parallel": 2,
    # worker pod resources (Kubernetes quantities, passed to the pod unchanged).
    # Measured use is 1.0-1.9 cores per worker pod, so 1500m requested lets four
    # workers plus an ems land pod (4 cpu) fit the 15-core dev node; the limit
    # still allows a burst to 4 cores. Applies to the next run's pod.
    "worker_cpu_request": "1500m",
    "worker_cpu_limit": "4",
    "worker_memory_request": "2Gi",
    "worker_memory_limit": "6Gi",
    # Claude Max: a model and an --effort level per phase. A model setting of
    # "" runs the phase on the story's own model; an effort of "" sends no
    # --effort flag. Effort is only ever sent to claude_code providers.
    # review_* cover both the AI review and the code review (one reviewer path).
    "spec_model": "claude-max/claude-opus-5-5",
    "review_model": "",
    "spec_effort": "high",
    "code_effort": "medium",
    "review_effort": "medium",
    # code review session: tool-call (turn) cap per session, wall clock per
    # session, and the budget for phase B (changed tests + mutations)
    "review_max_tool_calls": 40,
    "review_session_timeout_seconds": 900,
    "review_test_budget_seconds": 600,
}


def get_api_token(engine: Engine) -> str | None:
    """The stored API token, or None. Kept out of the merged settings dict."""
    with Session(engine) as db:
        row = db.get(AppSetting, API_TOKEN_KEY)
    return row.value if row and row.value else None


def set_api_token(engine: Engine, token: str | None) -> None:
    """Store (or clear, on None) the API token in its own AppSetting row."""
    with Session(engine) as db:
        row = db.get(AppSetting, API_TOKEN_KEY)
        if token is None:
            if row is not None:
                db.delete(row)
                db.commit()
            return
        if row is None:
            row = AppSetting(key=API_TOKEN_KEY, value="")
        row.value = token
        db.add(row)
        db.commit()


# deprecated key -> the key it now reads and writes (kept for one release):
# a stored value is read as the new key, a write to it writes the new key,
# and the merged settings echo it with the new key's value
DEPRECATED_ALIASES: dict[str, str] = {"max_parallel_stories": "model_slots"}


def _with_aliases(merged: dict[str, Any]) -> dict[str, Any]:
    return {**merged, **{old: merged[new] for old, new in DEPRECATED_ALIASES.items()}}


def load_settings(engine: Engine) -> dict[str, Any]:
    with Session(engine) as db:
        row = db.get(AppSetting, SETTINGS_KEY)
        stored = json.loads(row.value) if row and row.value else {}
    for old, new in DEPRECATED_ALIASES.items():
        if old in stored and new not in stored:
            stored[new] = stored[old]
    merged = {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}
    return _with_aliases(merged)


def save_settings(engine: Engine, partial: dict[str, Any]) -> dict[str, Any]:
    partial = dict(partial)
    for old, new in DEPRECATED_ALIASES.items():
        if old in partial:
            value = partial.pop(old)
            partial.setdefault(new, value)
    current = load_settings(engine)
    merged = {**current, **{k: v for k, v in partial.items() if k in DEFAULTS}}
    merged = {k: v for k, v in merged.items() if k in DEFAULTS}
    with Session(engine) as db:
        row = db.get(AppSetting, SETTINGS_KEY)
        if row is None:
            row = AppSetting(key=SETTINGS_KEY, value="{}")
        row.value = json.dumps(merged)
        db.add(row)
        db.commit()
    return _with_aliases(merged)


def prompt_defaults() -> dict[str, str]:
    return {field: (PROMPTS_DIR / name).read_text() for field, name in PROMPT_FILES.items()}


def effective_prompt(engine: Engine, field: str) -> str:
    override = load_settings(engine).get(field)
    if override and override.strip():
        return override
    return (PROMPTS_DIR / PROMPT_FILES[field]).read_text()
