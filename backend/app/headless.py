"""Headless Claude Code as a second CODE-stage executor.

A story (or project) whose effective execution is "headless" has its CODE
stage run by the Claude Code CLI in headless mode (`claude -p ...`) inside
the story's worktree, instead of the in-process pump agent. The CLI talks to
the same provider registry (base URL + stored key) that the pump uses, so
`zai/glm-5.3` via https://api.z.ai/api/anthropic is just a registry row.

Everything else in the pipeline — spec, verify, AI review, gates, landing —
is untouched: this module only replaces the `coder_round(...)` loop.

Live watch: the child runs with `--output-format stream-json --verbose` and
each stdout line is relayed to the caller through `on_event` while the run is
in flight, so the board shows the session as it happens. num_turns, usage
and duration all come from the stream's final `result` event; a run whose
stream has no result event is a failure.

Transcript: before the per-run CLAUDE_CONFIG_DIR is removed, the session
transcript JSONL under its `projects/` dir is copied to a per-story artifact
path (redacted) so a human can inspect the session after the fact. The path
is reported through the outcome.

Never ask: `--disallowedTools AskUserQuestion` and a prompt rule — the child
must decide instead of blocking on a question nobody answers.

Secrets: the provider key is passed to the child ONLY through its
environment. Every piece of text that leaves this module (stream events,
stdout summary, stderr tail, transcript, notes) is swept by the redactor
with the key as an extra literal, so a key echoed back by the CLI can never
land in an event, log or note.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import code_graph, procs
from .guardrails import redact_secrets

# The CLI binary invoked in headless mode. Tests put a fake `claude` script
# on PATH; production installs a pinned @anthropic-ai/claude-code (Dockerfile).
CLAUDE_BIN = "claude"

# Bounds for `headless_timeout_seconds`: the wall-clock budget for one child
# process. `tools_timeout_seconds` (the pump's per-tool-call bound) is NOT
# touched by this path and keeps its own setting.
HEADLESS_TIMEOUT_DEFAULT = 2400.0
HEADLESS_TIMEOUT_MIN = 30.0
HEADLESS_TIMEOUT_MAX = 7200.0

# API_TIMEOUT_MS handed to the child, as pinned by the spec. At the default
# (and up to a 3000 s headless_timeout_seconds) the CLI never times itself
# out first; above that our killpg still bounds the child — the child env
# value is a floor on patience, never the real bound.
API_TIMEOUT_MS = 3000000

# the low-skill/flash model used for the CLI's own background calls, with the
# provider prefix stripped the same way as the main model.
SMALL_FAST_MODEL_SUFFIX = "flash"

# caps for what we persist. The redactor sweep runs BEFORE the cap, so a
# truncated secret cannot sneak past.
FINAL_TEXT_CAP = 8 * 1024
STDERR_TAIL_CAP = 2 * 1024
# an assistant text block relayed live is capped much harder: it is a live
# progress line, not the deliverable
STREAM_TEXT_CAP = 2000
# a tool_use input summary: enough for a path/command, nothing more
STREAM_INPUT_CAP = 160

# the tool the child must never reach for: nobody is watching to answer
DISALLOWED_TOOLS = ("AskUserQuestion",)

# markers matched against the child's stderr to classify the failure
RATE_LIMIT_MARKERS = ("rate_limit", "ratelimit", "rate limit", "429", "1302")

# markers matched against the TEXT of a stream event flagged
# `isApiErrorMessage`: the 26 Oct incident shape, where the CLI exits 1 with
# empty stderr and the only 429/usage-limit evidence is an assistant event
# quoting "API Error: 429 ... rate_limit_error ... [1308][Usage limit
# reached for 5 hour. Your limit will reset at ...]".
STREAM_LIMIT_MARKERS = ("rate_limit_error", "usage limit", "429", "1302", "1308")
# "... Your limit will reset at 2026-10-26 19:29:59" — a NAIVE local
# timestamp in the provider's own zone (z.ai states it in UTC+8), never UTC.
STREAM_RESET_AT_RE = re.compile(r"reset at (\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")
# the human limit name inside the provider's bracketed "[1308][Usage limit
# reached for 5 hour. ...]" message
STREAM_LIMIT_NAME_RE = re.compile(r"\[\d{3,4}\]\[([^\].]+)")


@dataclass
class HeadlessOutcome:
    """Result of one headless CODE stage.

    outcome:
      ok       — exit 0, a result event parsed from the stream, under the
                 turn cap. `summary` carries the final text excerpt,
                 `num_turns`/`duration_ms`/usage for the coder-round-shaped
                 event. `transcript_path` points at the kept artifact.
      fix      — a real coder failure (non-zero exit, no result event, turn
                 cap, or timeout): the story must go back to the coder with
                 the reason, i.e. a fix round exactly like a pump coder error.
      transient — 429/rate-limit: a usage-limit answer in the stream
                 (isApiErrorMessage + marker; `limit_note`/`limit_reset_at`
                 carry the stated limit and reset time) or 429/rate-limit
                 stderr. Requeue with backoff, do NOT burn a fix round.
    """

    outcome: str  # "ok" | "fix" | "transient"
    reason: str | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    summary: str | None = None
    timed_out: bool = False
    exit_code: int | None = None
    transcript_path: str | None = None
    permission_denials: list[dict[str, Any]] = field(default_factory=list)
    # transient-only, from the stream's isApiErrorMessage line: the human
    # limit name ("Usage limit reached for 5 hour") and the stated reset
    # time converted to UTC. Both None on a plain stderr rate limit.
    limit_note: str | None = None
    limit_reset_at: datetime | None = None
    # A/B counter: mcp__code-review-graph__* tool uses in the session
    graph_tool_calls: int = 0


@dataclass
class HeadlessConfig:
    """Everything the child needs, resolved by the caller (never imported
    from app settings here, so tests can drive it directly)."""

    base_url: str | None
    api_key: str | None
    model: str  # canonical provider/model
    small_fast_model: str | None = None
    max_turns: int = 40
    timeout_seconds: float = HEADLESS_TIMEOUT_DEFAULT
    # per-story artifact dir for kept transcripts (the app's data dir); when
    # None no transcript is kept (unit tests without an app data dir)
    artifacts_dir: str | None = None
    # IANA zone the provider states usage-limit reset times in (the provider
    # row's limit_reset_tz; z.ai says UTC+8, so the default for zai is
    # Asia/Shanghai). UTC for providers that do not say otherwise.
    limit_reset_tz: str = "UTC"
    # Claude Max: the key is a Claude Code OAuth token (CLAUDE_CODE_OAUTH_TOKEN,
    # no base URL), not an API key
    oauth: bool = False
    # --effort level for a Claude Max run; None or "" sends no flag
    effort: str | None = None
    # inline JSON for --mcp-config (the code-review-graph server, see
    # code_graph.mcp_config_json); None = no MCP flags at all
    mcp_config: str | None = None


def strip_provider_prefix(canonical: str) -> str:
    """`zai/glm-5.3` -> `glm-5.3` (the CLI wants the bare model id)."""
    return canonical.split("/", 1)[1] if "/" in canonical else canonical


def flash_model_for(config: HeadlessConfig) -> str:
    """The ANTHROPIC_SMALL_FAST_MODEL value: an explicitly configured
    flash-tier model when the registry has one, else the same provider's
    `<prefix>-flash` sibling, else the bare suffix."""
    if config.small_fast_model:
        return strip_provider_prefix(config.small_fast_model)
    provider, sep, _ = config.model.partition("/")
    if not sep:
        return SMALL_FAST_MODEL_SUFFIX
    return f"{provider}/{SMALL_FAST_MODEL_SUFFIX}".split("/", 1)[1]


def _child_env(config: HeadlessConfig) -> dict[str, str]:
    """PATH/HOME/TMPDIR only, plus the Claude Code variables.

    Nothing else from the parent environment rides along — no FUBALA_*,
    no provider keys other than the one we deliberately inject. The token
    exists only in this dict and the child's memory.
    """
    env = _host_env()
    env.update(_claude_env(config))
    return env


def _host_env() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
    }
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        env["TMPDIR"] = tmpdir
    return env


def pod_child_env(config: HeadlessConfig, config_dir: str) -> dict[str, str]:
    """The worker-pod variant: the Claude Code variables plus the round's
    CLAUDE_CONFIG_DIR, and nothing of the backend (no PATH/HOME/TMPDIR: the
    pod's own POD_ENV applies). Sent over exec stdin only, never in argv."""
    return {**_claude_env(config), "CLAUDE_CONFIG_DIR": config_dir}


def _claude_env(config: HeadlessConfig) -> dict[str, str]:
    env: dict[str, str] = {}
    if config.oauth:
        # a subscription token: the CLI talks to Anthropic itself, so no base
        # URL and no ANTHROPIC_AUTH_TOKEN (that would be sent as an API key)
        if config.api_key:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = config.api_key
    else:
        if config.base_url:
            env["ANTHROPIC_BASE_URL"] = config.base_url
        if config.api_key:
            env["ANTHROPIC_AUTH_TOKEN"] = config.api_key
    env["ANTHROPIC_MODEL"] = strip_provider_prefix(config.model)
    env["ANTHROPIC_SMALL_FAST_MODEL"] = flash_model_for(config)
    env["API_TIMEOUT_MS"] = str(API_TIMEOUT_MS)
    env["DISABLE_TELEMETRY"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def tool_neutral_rules() -> list[str]:
    """The rules appended to every headless prompt, verbatim.

    Tool-neutral on purpose: the CLI picks its own tools; we only pin the
    workflow contract (tests while working, full verify before finishing,
    never commit, never leave scratch files, never ask questions).
    """
    return [
        "While working, run the project's test command for the files you "
        "touch (`sh tools/test.sh <paths>` when the project provides it).",
        "Run the full verify command before you finish.",
        "Do not commit; the pipeline commits for you.",
        "Leave no scratch files behind — only changes the spec requires.",
        "Never ask questions; decide, and state your assumptions in the "
        "final text.",
    ]


def with_rules(
    prompt: str,
    *,
    test_command: str | None = None,
    verify_command: str | None = None,
) -> str:
    """Append the tool-neutral rules to a runner-built CODE REQUEST.

    The runner assembles the CODE REQUEST itself (spec, previous feedback,
    human reject reasons, the test-command line) because the pump and the
    headless path share that assembly. This function is the one place the
    headless-specific contract is attached: the CLI picks its own tools, so
    the rules are tool-neutral and name the concrete commands instead.

    Idempotent by marker: a prompt that already carries the rules section is
    returned unchanged (a retried round must not grow a second copy).
    """
    if "## Rules" in prompt:
        return prompt
    # the pump's TESTS block names run_tests/run_verify tools the CLI does
    # not have; drop it so only tool-neutral rules remain. Exact three-line
    # shape (see runner.test_command_prompt_line), never a greedy sweep.
    prompt = re.sub(
        r"=== TESTS ===\n[^\n]*\n[^\n]*for the full suite\.\n?", "", prompt
    )
    lines = ["## Rules"]
    if test_command:
        lines.append(
            f"While working, run `sh tools/test.sh <paths>` for the files "
            f"you touch; this project's test command is `{test_command}`."
        )
    else:
        lines.append(
            "While working, run the project's test command for the files you touch."
        )
    if verify_command:
        lines.append(
            f"Run the full verify command before you finish: `{verify_command}`."
        )
    else:
        lines.append("Run the full verify command before you finish.")
    lines.append("Do not commit; the pipeline commits for you.")
    lines.append("Leave no scratch files behind — only changes the spec requires.")
    lines.append(
        "Never ask questions; decide, and state your assumptions in the "
        "final text."
    )
    return prompt + "\n\n" + "\n".join(lines)


def build_prompt(
    *,
    title: str,
    spec: str,
    feedback: str | None = None,
    human_reasons: list[str] | None = None,
    test_command: str | None = None,
    verify_command: str | None = None,
) -> str:
    """The pump's CODE REQUEST, re-shaped for a single-shot headless call."""
    parts = [
        "=== CODE REQUEST ===",
        f"# Story: {title}",
        "## Spec to implement",
        spec,
    ]
    if feedback:
        parts += ["## Previous attempt feedback (fix this too)", feedback]
    if human_reasons:
        parts += [
            "## Human reject reasons (every point must be addressed)",
            *(f"{i}. {r}" for i, r in enumerate(human_reasons, 1)),
        ]
    if test_command:
        parts += [f"Test command for this project: {test_command}"]
    if verify_command:
        parts += [f"Verify command for this project: {verify_command}"]
    parts += ["## Rules", *tool_neutral_rules()]
    return "\n".join(parts)


def child_argv(prompt: str, config: HeadlessConfig) -> list[str]:
    """The exact argv the CLI is invoked with (stream-json + verbose for the
    live watch, AskUserQuestion disallowed so the child can never block on a
    question nobody answers)."""
    argv = [
        CLAUDE_BIN,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
        "--disallowedTools",
        *DISALLOWED_TOOLS,
        "--max-turns",
        str(max(1, int(config.max_turns))),
    ]
    if config.oauth and config.effort:
        argv += ["--effort", config.effort]
    if config.mcp_config:
        argv += ["--mcp-config", config.mcp_config, "--strict-mcp-config"]
    return argv


def _kill_process_group(pid: int) -> None:
    """SIGKILL the child's whole process group (start_new_session=True put
    the child in its own). Best-effort: a child that already exited raises
    ProcessLookupError, which is exactly the outcome we wanted."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass


def _redact(text: str, extra_literals: tuple[str, ...] = ()) -> str:
    """The guardrails sweep, plus explicit literals (the provider key) that
    the generic patterns may not know."""
    redacted = redact_secrets(text)
    for literal in extra_literals:
        if literal:
            redacted = redacted.replace(literal, "[REDACTED]")
    return redacted


def _clip(text: str, cap: int) -> str:
    """Redaction-safe clip: never split a secret's tail into view.

    The redactor already ran over the whole text by the time we clip, so a
    plain prefix cut is safe; the `…` marks the cut for the reader.
    """
    if len(text) <= cap:
        return text
    return text[: cap - 1] + "…"


def _usage_from(result: dict[str, Any]) -> dict[str, Any]:
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return {}
    out: dict[str, Any] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, int):
            out[key] = value
    return out


def _denials_from(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Visible blocked tools: a non-empty `permission_denials` becomes a
    note, never a silent ok."""
    denials = result.get("permission_denials")
    if not isinstance(denials, list):
        return []
    out: list[dict[str, Any]] = []
    for item in denials:
        if isinstance(item, str):
            out.append({"tool": item, "input": ""})
        elif isinstance(item, dict):
            tool = item.get("tool") or item.get("name") or ""
            summary = item.get("input_summary") or item.get("input") or ""
            if not isinstance(tool, str):
                tool = json.dumps(tool)[:STREAM_INPUT_CAP]
            if not isinstance(summary, str):
                summary = json.dumps(summary)[:STREAM_INPUT_CAP]
            out.append({"tool": tool[:STREAM_INPUT_CAP], "input": summary[:STREAM_INPUT_CAP]})
    return out[:20]


# ---- stream-json handling -------------------------------------------------


def parse_stream_line(line: str) -> dict[str, Any] | None:
    """One stdout line -> the parsed JSON object, or None for noise.

    The CLI prefixes stream lines with nothing, but a stray banner or an
    empty line must not kill the relay; `None` means "skip".
    """
    text = line.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def summarize_stream_event(
    event: dict[str, Any], secrets: tuple[str, ...] = ()
) -> dict[str, Any] | None:
    """A parsed stream event -> the small, redacted relay payload.

    Returns None for events that carry nothing worth watching. The payload
    shapes mirror the pump's activity feed (`tool_call` / `tool_result`) so
    the board renders both executors the same way:

      assistant   -> {"type": "assistant", "text": <capped redacted text>}
      tool_use    -> {"type": "tool_use", "tool": name, "input": summary}
      tool_result -> {"type": "tool_result", "status": ok|error,
                      "chars": n}
      result      -> None (handled by the caller; the summary event is
                     published from the outcome, with turns/usage/duration)
    """
    kind = event.get("type")
    if kind == "assistant":
        message = event.get("message")
        if not isinstance(message, dict):
            return None
        chunks: list[str] = []
        for block in _content_blocks(message):
            if block.get("type") == "text":
                value = block.get("text")
                if isinstance(value, str) and value.strip():
                    chunks.append(value)
        if not chunks:
            return None
        text = _clip(_redact("\n".join(chunks), secrets), STREAM_TEXT_CAP)
        return {"type": "assistant", "text": text}
    if kind == "user":
        message = event.get("message")
        if not isinstance(message, dict):
            return None
        for block in _content_blocks(message):
            if block.get("type") != "tool_result":
                continue
            content = block.get("content")
            chars = len(content) if isinstance(content, (str, list)) else 0
            payload = {
                "type": "tool_result",
                "status": "error" if block.get("is_error") else "ok",
                "chars": chars,
            }
            if isinstance(content, str):
                payload["preview"] = _clip(
                    _redact(content, secrets), STREAM_INPUT_CAP
                )
            return payload
        return None
    return None


def _parse_limit_reset(text: str, zone_name: str) -> datetime | None:
    """The stated reset time as a UTC-aware datetime, or None.

    The provider states it NAIVE in its own zone (z.ai: UTC+8 — read as UTC
    the hold would last 8 hours too long), so it is interpreted in
    `zone_name` before conversion. An unresolvable zone name falls back to
    UTC rather than dropping the evidence.
    """
    match = STREAM_RESET_AT_RE.search(text)
    if not match:
        return None
    try:
        naive = datetime.strptime(match.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    try:
        zone: Any = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    return naive.replace(tzinfo=zone).astimezone(UTC)


def stream_rate_limit(
    events: Iterable[dict[str, Any]], *, tz: str = "UTC"
) -> tuple[str, datetime | None] | None:
    """Find a usage-limit answer in the stream: an event flagged
    `isApiErrorMessage` whose text carries a limit marker.

    Returns `(limit_name, reset_at_utc)` for the first match — reset may be
    None when the provider stated no time — or None when the stream carries
    no such evidence. The flag AND a marker must both be present: ordinary
    assistant text merely quoting "API Error" must never requeue a story.
    """
    for event in events:
        if not event.get("isApiErrorMessage"):
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        for block in _content_blocks(message):
            if block.get("type") != "text":
                continue
            text = block.get("text")
            if not isinstance(text, str):
                continue
            lowered = text.lower()
            if not any(marker in lowered for marker in STREAM_LIMIT_MARKERS):
                continue
            name_match = STREAM_LIMIT_NAME_RE.search(text)
            if name_match:
                name = name_match.group(1).strip()
            elif "usage limit" in lowered:
                name = "usage limit"
            else:
                name = "rate limit"
            return name, _parse_limit_reset(text, tz)
    return None


def tool_use_summaries(event: dict[str, Any]) -> list[dict[str, Any]]:
    """The tool_use blocks an assistant message carries (name + short input
    summary), for the live relay."""
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    out: list[dict[str, Any]] = []
    for block in _content_blocks(message):
        if block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if not isinstance(name, str):
            name = "?"
        summary = ""
        raw_input = block.get("input")
        if isinstance(raw_input, dict):
            for key in ("path", "command", "file_path", "pattern", "url"):
                value = raw_input.get(key)
                if isinstance(value, str) and value:
                    summary = value
                    break
            if not summary:
                summary = json.dumps(raw_input, default=str)
        elif isinstance(raw_input, str):
            summary = raw_input
        out.append(
            {"type": "tool_use", "tool": name, "input": summary[:STREAM_INPUT_CAP]}
        )
    return out


class StreamReader:
    """Per-line stream-json handling, shared by the local and the pod path:
    parse, keep the `result` event, relay tool_use summaries and the
    summarized event. `relay` must never raise (see `safe_relay`)."""

    def __init__(
        self, secrets: tuple[str, ...], relay: Callable[[dict[str, Any]], None]
    ) -> None:
        self.secrets = secrets
        self.relay = relay
        self.events: list[dict[str, Any]] = []
        self.result_event: dict[str, Any] | None = None

    def feed(self, line: str) -> None:
        parsed = parse_stream_line(line)
        if parsed is None:
            return
        self.events.append(parsed)
        if parsed.get("type") == "result":
            # the final event: parsed for turns/usage/result by map_outcome
            self.result_event = parsed
            return
        for summary in tool_use_summaries(parsed):
            self.relay(summary)
        summary_event = summarize_stream_event(parsed, self.secrets)
        if summary_event is not None:
            self.relay(summary_event)


def safe_relay(
    on_event: Callable[[dict[str, Any]], None] | None,
) -> Callable[[dict[str, Any]], None]:
    def relay(payload: dict[str, Any]) -> None:
        if on_event is not None:
            try:
                on_event(payload)
            except Exception:  # noqa: BLE001 - a listener must not kill the run
                pass

    return relay


def run_secrets(config: HeadlessConfig) -> tuple[str, ...]:
    return tuple(s for s in (config.api_key,) if s)


def timeout_outcome(duration_ms: int) -> HeadlessOutcome:
    return HeadlessOutcome(
        outcome="fix",
        reason="headless timeout",
        timed_out=True,
        duration_ms=duration_ms,
    )


def map_outcome(
    *,
    exit_code: int | None,
    stderr: str,
    stream: StreamReader,
    config: HeadlessConfig,
    duration_ms: int,
    transcript_path: Path | None,
) -> HeadlessOutcome:
    """Map a finished child (exit code, stderr, parsed stream) to an outcome.
    Shared by the local and the pod path."""
    secrets = stream.secrets
    result_event = stream.result_event
    if exit_code != 0:
        # stream evidence FIRST: the 26 Oct incident exits 1 with empty
        # stderr and the 429/usage-limit answer only in an isApiErrorMessage
        # stream event — without this check that shape lands in "fix" and
        # fails the story (and cascades to its dependents).
        detected = stream_rate_limit(stream.events, tz=config.limit_reset_tz)
        if detected is not None:
            # the note is child output leaving this module: sweep it like
            # every other piece of text the run produces
            note, reset_at = detected
            note = _redact(note, secrets)
            reason = f"rate limit (headless): {note}"
            if reset_at is not None:
                reason += f"; resets at {reset_at.isoformat()}"
            return HeadlessOutcome(
                outcome="transient",
                reason=reason,
                duration_ms=duration_ms,
                exit_code=exit_code,
                usage={},
                transcript_path=str(transcript_path) if transcript_path else None,
                limit_note=note,
                limit_reset_at=reset_at,
            )
        lowered = stderr.lower()
        if any(marker in lowered for marker in RATE_LIMIT_MARKERS):
            return HeadlessOutcome(
                outcome="transient",
                reason="rate limit (headless)",
                duration_ms=duration_ms,
                exit_code=exit_code,
                usage={},
                transcript_path=str(transcript_path) if transcript_path else None,
            )
        tail = _clip(_redact(stderr, secrets).strip(), STDERR_TAIL_CAP)
        return HeadlessOutcome(
            outcome="fix",
            reason=f"headless exit {exit_code}: {tail or '(no stderr)'}",
            duration_ms=duration_ms,
            exit_code=exit_code,
            transcript_path=str(transcript_path) if transcript_path else None,
        )

    if result_event is None:
        tail = _clip(_redact(stderr, secrets).strip(), STDERR_TAIL_CAP)
        return HeadlessOutcome(
            outcome="fix",
            reason="headless stream ended without a result event: "
            + (tail or "(no stderr)"),
            duration_ms=duration_ms,
            exit_code=exit_code,
            transcript_path=str(transcript_path) if transcript_path else None,
        )

    num_turns_raw = result_event.get("num_turns")
    try:
        num_turns = int(num_turns_raw) if num_turns_raw is not None else None
    except (TypeError, ValueError):
        num_turns = None
    if num_turns is not None and num_turns >= max(1, int(config.max_turns)):
        # at the cap the CLI stopped without finishing: a failure, not a
        # success (the spec's explicit rule)
        return HeadlessOutcome(
            outcome="fix",
            reason=f"headless hit the --max-turns cap ({num_turns})",
            num_turns=num_turns,
            duration_ms=duration_ms,
            exit_code=exit_code,
            transcript_path=str(transcript_path) if transcript_path else None,
        )

    denials = _denials_from(result_event)
    final_text = result_event.get("result")
    if not isinstance(final_text, str):
        final_text = ""
    return HeadlessOutcome(
        outcome="ok",
        num_turns=num_turns,
        duration_ms=duration_ms,
        usage=_usage_from(result_event),
        summary=_clip(_redact(final_text, secrets), FINAL_TEXT_CAP),
        exit_code=exit_code,
        transcript_path=str(transcript_path) if transcript_path else None,
        permission_denials=denials,
        graph_tool_calls=code_graph.count_graph_tool_calls(stream.events),
    )


# ---- transcript retention -------------------------------------------------


def find_transcript(config_dir: Path) -> Path | None:
    """The newest session transcript JSONL under the config dir's
    `projects/` tree, or None when the child wrote none."""
    projects_dir = config_dir / "projects"
    if not projects_dir.is_dir():
        return None
    candidates = sorted(
        (p for p in projects_dir.rglob("*.jsonl") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def keep_transcript(
    source: Path, story_id: str, artifacts_dir: Path, secrets: tuple[str, ...]
) -> Path:
    """Copy the session transcript to `<artifacts>/headless/<story>/<n>.jsonl`,
    redacting every line in flight. Returns the destination path."""
    target_dir = artifacts_dir / "headless" / _safe(story_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    target = target_dir / f"{stamp}-{os.getpid()}.jsonl"
    n = 0
    with open(source, encoding="utf-8", errors="replace") as src, open(
        target, "w", encoding="utf-8"
    ) as dst:
        for line in src:
            dst.write(_redact(line, secrets))
            n += 1
    if n == 0:
        target.write_text("(the session transcript was empty)\n")
    return target


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80] or "story"


# ---- the CODE stage -------------------------------------------------------


def run_code_stage(
    *,
    story_id: str,
    worktree: Path,
    prompt: str,
    config: HeadlessConfig,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> HeadlessOutcome:
    """Run one headless CODE stage and map the child's fate to an outcome.

    Synchronous on purpose: the runner calls it via `asyncio.to_thread` (the
    same pattern as git worktree add/del and verify), keeping the event loop
    free for SSE, health and other stories. `on_event` is called from this
    thread for every relayed stream event — the runner publishes it to the
    board from there.
    """
    secrets = run_secrets(config)
    stream = StreamReader(secrets, safe_relay(on_event))

    started = time.monotonic()
    config_dir = Path(tempfile.mkdtemp(prefix=f"fubala-headless-{_safe(story_id)}-"))
    env = _child_env(config)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    argv = child_argv(prompt, config)
    transcript_path: Path | None = None
    stderr = ""
    child: subprocess.Popen | None = None
    child_pgid: int | None = None
    reap_reason = "round_end"
    try:
        # own session + Popen+register under the module lock (app.procs): the
        # tree is killable by cancel/stop and the /proc sweep, and can never
        # be swept between its first /proc appearance and its registration
        child, pgid = procs.popen(
            argv,
            story_id=story_id,
            cwd=str(worktree),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        child_pgid = pgid
        try:
            # stdout is the stream-json channel; read it line by line so the
            # board sees the session while the child works. stderr is drained
            # by a reader thread so a chatty child can never block on a full
            # pipe while we are reading stdout. The wall-clock deadline is
            # enforced HERE, on the read loop — a child that never speaks and
            # never exits must still be bounded by headless_timeout_seconds.
            stderr_chunks: list[bytes] = []

            def drain_stderr() -> None:
                assert child.stderr is not None
                for chunk in iter(child.stderr.readline, b""):
                    stderr_chunks.append(chunk)

            stderr_reader = threading.Thread(target=drain_stderr, daemon=True)
            stderr_reader.start()

            lines: queue.Queue[bytes | None] = queue.Queue()

            def drain_stdout() -> None:
                assert child.stdout is not None
                try:
                    for raw in iter(child.stdout.readline, b""):
                        lines.put(raw)
                finally:
                    lines.put(None)

            stdout_reader = threading.Thread(target=drain_stdout, daemon=True)
            stdout_reader.start()

            deadline = started + max(1.0, float(config.timeout_seconds))
            timed_out = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    raw = lines.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                if raw is None:
                    break
                stream.feed(raw.decode("utf-8", "replace"))
            if timed_out:
                raise subprocess.TimeoutExpired(cmd=argv[0], timeout=0)
            child.wait(timeout=max(1.0, deadline - time.monotonic()))
            stderr_reader.join(timeout=5)
            stdout_reader.join(timeout=5)
            stderr = b"".join(stderr_chunks).decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            reap_reason = "timeout"
            _kill_process_group(child.pid)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            return timeout_outcome(int((time.monotonic() - started) * 1000))
    except FileNotFoundError:
        return HeadlessOutcome(
            outcome="fix",
            reason=f"{CLAUDE_BIN} executable not found on PATH",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    finally:
        # the headless tree is gone (or never started): drop it from the
        # process-group registry so cancel/stop/sweep stop tracking it
        if child is not None and child_pgid is not None:
            # kill what the round left running (setsid escapees) first
            procs.reap_run(
                story_id,
                getattr(child, "fubala_run_id", None),
                child_pgid,
                reason=reap_reason,
            )
            procs.unregister(story_id, child_pgid)
        # KEEP THE TRANSCRIPT before the config dir goes away: copy the
        # session JSONL (redacted) to the per-story artifact path
        try:
            source = find_transcript(config_dir)
            if source is not None and config.artifacts_dir:
                transcript_path = keep_transcript(
                    source, story_id, Path(config.artifacts_dir), secrets
                )
        except OSError:
            transcript_path = None
        shutil.rmtree(config_dir, ignore_errors=True)

    return map_outcome(
        exit_code=child.returncode,
        stderr=stderr,
        stream=stream,
        config=config,
        duration_ms=int((time.monotonic() - started) * 1000),
        transcript_path=transcript_path,
    )
