"""The agent loop: model streams, requests tools, we execute and continue.

Keeps the Auto-Claude lesson of ONE pump: this is the only place the
tool round-trip exists; chat and the story runner both ride on it.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .providers import (
    Delta,
    GatewayError,
    ProviderGateway,
    StreamIdleTimeout,
    ToolCall,
    Usage,
)

log = logging.getLogger(__name__)

MAX_ROUNDS = 160
MAX_TRUNCATION_RETRIES = 3
# consecutive stalled streams (StreamIdleTimeout) repeated in-round before the
# error propagates and the runner requeues the story with backoff
MAX_STREAM_STALL_RETRIES = 3
# empty-round nudges before the phase is declared silent (see _meaningful)
MAX_EMPTY_RETRIES = 3
# an output-cap hit may be escalated (budget doubled) this many times before
# the round falls through to the ordinary empty-round nudge path
MAX_OUTPUT_ESCALATIONS = 2
# hard ceiling for any single round's output budget
MAX_OUTPUT_TOKENS_CEILING = 32768
CAP_FINISH_REASONS = ("length", "max_tokens")

# z.ai's anthropic-compatible endpoint truncates very large single tool-call
# argument JSON; litellm surfaces it as a parse error (fubala-prompts/006)
TRUNCATION_MARKERS = (
    "failed to parse tool call arguments",
    "unterminated string",
    "unterminated array",
    "unexpected end of json",
)
TRUNCATION_GUIDANCE = (
    "[system] Your last tool call was too large for the provider and arrived "
    "truncated, so it was NOT executed. Apply the change in smaller pieces: "
    "use edit_file for targeted section edits on existing large files, and "
    "write_file only for new files or small complete rewrites. If a file is "
    "very large, split it into smaller modules. Then continue the task."
)

# A 400 "Failed to parse tool call arguments" where the model simply emitted
# garbage JSON (incident e56b558d): the assistant turn sitting in the
# conversation carries arguments no one can parse, so every echo of the
# history fails identically. The turn must be DROPPED, not annotated — the
# truncation guidance above cannot fix it (it keeps the poison turn in).
BAD_TOOL_ARGS_MARKER = "failed to parse tool call arguments"
# truncation sub-markers: when one appears WITHOUT a poison assistant turn
# the error is the 006 truncation case, and the guidance path keeps it
TRUNCATION_ONLY_MARKERS = ("unterminated string", "unterminated array", "unexpected end of json")
MAX_BAD_TOOL_ARGS_CORRECTIONS = 2
# "<name>" filled in from the provider error (or the dropped turn) at use
BAD_TOOL_ARGS_TOOL_RE = re.compile(r"for tool ['\"]([^'\"]+)")

BAD_TOOL_ARGS_CORRECTION_TEMPLATE = (
    "[system] your previous tool call had invalid JSON arguments for tool "
    "{tool_name}; call it again with valid JSON"
)


def bad_tool_args_correction(tool_name: str) -> str:
    """The corrective user turn appended after dropping the offending
    assistant message. Valid JSON next time, or the round fails."""
    return BAD_TOOL_ARGS_CORRECTION_TEMPLATE.format(tool_name=tool_name)


def _unparsable_calls(message: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The tool_calls of `message` whose `arguments` are not valid JSON.

    An empty list means: not an assistant turn, no tool calls, or every call
    parses — i.e. nothing to drop for a bad-args correction."""
    if not message or message.get("role") != "assistant":
        return []
    unparsable: list[dict[str, Any]] = []
    for call in message.get("tool_calls") or []:
        try:
            json.loads(call.get("function", {}).get("arguments", ""))
        except (ValueError, TypeError):
            unparsable.append(call)
    return unparsable


def _last_bad_tool_args(msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Unparsable tool calls of the MOST RECENT assistant turn.

    Tool results sit after the assistant turn they answer, so the echo case
    (incident e56b558d) hides the poison one message back: scan backwards and
    stop at the first assistant turn. Older turns are never considered — a
    bad turn deeper in history was already answered and is not this error."""
    for message in reversed(msgs):
        bad = _unparsable_calls(message)
        if bad:
            return bad
        if message.get("role") == "assistant":
            return []
    return []

def is_placeholder_line(line: str) -> bool:
    """True for the '[System: …]' provider placeholder lines. One predicate
    shared by `_meaningful` and `strip_placeholders` so the two can never
    drift apart."""
    return line.strip().startswith("[" + "System:")


def _meaningful(text: str) -> bool:
    """z.ai's anthropic endpoint emits literal '[System: …]' placeholder
    text for thinking-only turns; placeholder-only output is silence."""
    return bool(
        [
            line
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("[System:")
        ]
    )


def strip_placeholders(text: str | None) -> str:
    """Drop provider placeholder lines ('[System: …]') from a deliverable.

    Same predicate as `_meaningful`: a line whose stripped form starts with
    '[System:' carries no information. Blank lines survive so the shape of
    the text is preserved; callers needing emptiness semantics compare
    against ``strip()`` afterwards (see runner._spec_preflight).

    Round-trip safe: an already-clean text comes back byte-for-byte
    identical, trailing newline included ('x\\n' -> 'x\\n', not 'x'), so
    storing a deliverable never mutates it (pinned by test_spec_preflight).
    """
    if not text:
        return ""
    if not any(is_placeholder_line(line) for line in text.splitlines()):
        return text
    kept = [line for line in text.splitlines() if not is_placeholder_line(line)]
    # a trailing newline survives the strip, so a clean tail is not mutated
    return "\n".join(kept) + ("\n" if text.endswith("\n") else "")


@dataclass
class AgentResult:
    text: str
    usage: Usage | None
    rounds: int
    tool_calls: int
    # False when the phase never produced a real answer: the model spent its
    # turn thinking (or hit the output cap) on every round. Callers must NOT
    # treat `text` as a deliverable when this is False — it is narration from
    # an earlier tool round at best, and "" once narration caching is off.
    complete: bool = True


def _cap_hit(usage: Usage | None, budget: int | None) -> bool:
    """Did this round stop because the output budget ran out? Either the
    provider says so (finish_reason length/max_tokens) or the round burned
    every token it was allowed."""
    if usage is None:
        return False
    if (usage.finish_reason or "").lower() in CAP_FINISH_REASONS:
        return True
    return bool(budget) and usage.completion_tokens >= budget


async def run_agent(
    gateway: ProviderGateway,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    execute: Callable[[str, str], str] | None = None,
    on_delta: Callable[[str], None] | None = None,
    on_tool: Callable[[str, str, str], None] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    on_tool_start: Callable[[str, str, str], None] | None = None,
    on_tool_end: Callable[[str], None] | None = None,
    on_correction: Callable[[str, int], None] | None = None,
    max_tool_calls: int | None = None,
    timeout_seconds: float | None = None,
    max_output_tokens: int | None = None,
    max_output_tokens_cap: int | None = None,
    effort: str | None = None,
    log_context: str | None = None,
) -> AgentResult:
    """Stream a completion; whenever the model requests tools, execute them,
    append the results, and continue — until plain text or MAX_ROUNDS.

    `on_progress` fires at the START of each round with a snapshot of the
    budget: round/max_rounds, tool_calls/max_tool_calls, elapsed_s/timeout_s
    and cumulative prompt/completion tokens summed across rounds. Omitting it
    changes nothing.

    `max_output_tokens` caps each round's completion budget; a round that
    stops with no meaningful text AND a length/max_tokens finish (or
    completion tokens at the cap) is retried once at double the budget
    (ceiling 32768, max 2 escalations) before the ordinary empty-round
    nudging kicks in.

    `max_output_tokens_cap` (settings: agent_max_output_tokens_cap) clamps the
    escalation ceiling below the built-in 32768 for operators who pay per
    token; it can never raise the ceiling.

    `on_correction` fires when a malformed tool-call 400 is repaired in-round
    (the offending assistant turn dropped, a corrective user message
    appended): callback arguments are (tool name, round number). Omitting it
    changes nothing; the correction still happens.

    A round whose stream stalls (StreamIdleTimeout) is repeated with the same
    messages, up to MAX_STREAM_STALL_RETRIES in a row; `log_context` (the story
    id) only labels that warning."""
    msgs: list[dict] = [dict(m) for m in messages]
    usage: Usage | None = None
    total_calls = 0
    budget_out = max_tool_calls is not None or timeout_seconds is not None
    started = time.monotonic()
    deadline = started + timeout_seconds if timeout_seconds else None
    prompt_tokens = 0
    completion_tokens = 0
    empty_retries = 0
    truncation_retries = 0
    bad_args_corrections = 0
    stream_stalls = 0
    escalations = 0

    for round_no in range(1, MAX_ROUNDS + 1):

        def snapshot() -> dict[str, Any]:
            return {
                "round": round_no,
                "max_rounds": MAX_ROUNDS,
                "tool_calls": total_calls,
                "max_tool_calls": max_tool_calls,
                "elapsed_s": round(time.monotonic() - started, 3),
                "timeout_s": timeout_seconds,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }

        if on_progress is not None:
            on_progress(snapshot())
        parts: list[str] = []
        calls: list[ToolCall] = []
        round_tools = tools
        if budget_out and round_no > 1:
            if (max_tool_calls is not None and total_calls >= max_tool_calls) or (
                deadline is not None and time.monotonic() > deadline
            ):
                round_tools = None  # budget spent: the model must now answer
        try:
            # only forwarded when set: gateways without an effort parameter
            # (and every non-claude_code path) never see the kwarg
            extra = {"effort": effort} if effort is not None else {}
            async for event in gateway.stream_chat(
                model=model,
                messages=msgs,
                tools=round_tools,
                max_output_tokens=max_output_tokens,
                **extra,
            ):
                if isinstance(event, Delta):
                    parts.append(event.text)
                    if on_delta:
                        on_delta(event.text)
                elif isinstance(event, ToolCall):
                    calls.append(event)
                elif isinstance(event, Usage):
                    usage = event
                    # cumulative across rounds: gateways report per-round usage
                    prompt_tokens += event.prompt_tokens
                    completion_tokens += event.completion_tokens
        except GatewayError as e:
            if isinstance(e, StreamIdleTimeout):
                if stream_stalls >= MAX_STREAM_STALL_RETRIES:
                    raise
                stream_stalls += 1
                log.warning(
                    "stream idle: story=%s model=%s round=%d idle=%ss, retrying (%d/%d)",
                    log_context,
                    model,
                    round_no,
                    getattr(gateway, "stream_idle_timeout", None),
                    stream_stalls,
                    MAX_STREAM_STALL_RETRIES,
                )
                continue
            lowered = str(e).lower()
            bad_args = _last_bad_tool_args(msgs)
            if (
                BAD_TOOL_ARGS_MARKER in lowered
                # the OR clause is the discriminator against the 006
                # truncation path: a truncation sub-marker WITHOUT a poison
                # assistant turn is truncation (guidance); WITH one it is a
                # malformed-args echo that guidance can never fix
                and (
                    not any(m in lowered for m in TRUNCATION_ONLY_MARKERS)
                    or bad_args
                )
            ):
                # the model's own in-round mistake: drop the offending
                # assistant turn (and its tool results) and tell it to call
                # the tool properly. Bounded per round so a model stuck in
                # the failure still fails the story via the re-raise.
                if bad_args_corrections >= MAX_BAD_TOOL_ARGS_CORRECTIONS:
                    raise
                while msgs and msgs[-1].get("role") == "tool":
                    msgs.pop()
                dropped = msgs.pop() if _last_bad_tool_args(msgs) else None
                dropped_args = _unparsable_calls(dropped)
                match = BAD_TOOL_ARGS_TOOL_RE.search(str(e))
                if match:
                    tool_name = match.group(1)
                elif dropped_args:
                    tool_name = dropped_args[0].get("function", {}).get("name", "unknown")
                else:
                    tool_name = "unknown"
                msgs.append(
                    {"role": "user", "content": bad_tool_args_correction(tool_name)}
                )
                bad_args_corrections += 1
                if on_correction is not None:
                    on_correction(tool_name, round_no)
                continue
            if (
                any(m in lowered for m in TRUNCATION_MARKERS)
                and truncation_retries < MAX_TRUNCATION_RETRIES
            ):
                # retryable-with-guidance, not a story failure (006 AC1+AC2)
                truncation_retries += 1
                msgs.append({"role": "assistant", "content": "(truncated tool call omitted)"})
                msgs.append({"role": "user", "content": TRUNCATION_GUIDANCE})
                continue
            raise

        text = "".join(parts)
        # a round that streamed cleanly re-arms the correction budget: the
        # cap is per round (consecutive corrections), not per run
        bad_args_corrections = 0
        stream_stalls = 0
        if not calls and not _meaningful(text):
            # some models (GLM-5.3 via the anthropic protocol) emit thinking-
            # only rounds that surface as placeholder text and NO tool calls.
            # Silence is not an answer — nudge the model to continue instead
            # of ending the run with nothing (the "coder produced no
            # changes" loop of fubala-prompts/005).
            if _cap_hit(usage, max_output_tokens) and escalations < MAX_OUTPUT_ESCALATIONS:
                # the model spent its whole budget thinking: give it more
                # room rather than nudging it to say something shorter
                escalations += 1
                ceiling = (
                    min(max_output_tokens_cap, MAX_OUTPUT_TOKENS_CEILING)
                    if max_output_tokens_cap
                    else MAX_OUTPUT_TOKENS_CEILING
                )
                new_budget = min(
                    max_output_tokens * 2 if max_output_tokens else ceiling,
                    ceiling,
                )
                if on_progress is not None:
                    # surfaced as a "round" activity row so the operator can
                    # SEE the escalation instead of watching silence
                    on_progress(
                        {
                            **snapshot(),
                            "cap_escalation": True,
                            "max_output_tokens": new_budget,
                        }
                    )
                max_output_tokens = new_budget
                msgs.append({"role": "assistant", "content": "(no output)"})
                msgs.append(
                    {
                        "role": "user",
                        "content": (
                            "[system] Your previous turn hit the output token "
                            "limit before producing text. Continue the task "
                            "from where you stopped; be concise."
                        ),
                    }
                )
                continue
            if empty_retries >= MAX_EMPTY_RETRIES:
                # Exhausted nudging: there is no answer. Returning cached
                # narration here would smuggle stale tool-round chatter in as
                # the deliverable, so the phase ends silent AND incomplete.
                return AgentResult("", usage, round_no, total_calls, complete=False)
            empty_retries += 1
            msgs.append({"role": "assistant", "content": "(no output)"})
            msgs.append({
                "role": "user",
                "content": (
                    "[system] Your previous turn produced no output and no tool "
                    "calls. Continue the task: call a tool, or give your final "
                    "answer."
                ),
            })
            continue
        empty_retries = 0

        if not calls or execute is None:
            return AgentResult("".join(parts), usage, round_no, total_calls)

        # record the assistant's tool requests, then each tool result
        msgs.append(
            {
                "role": "assistant",
                "content": "".join(parts),
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in calls
                ],
            }
        )
        for index, call in enumerate(calls):
            total_calls += 1
            call_id = call.id or f"r{round_no}-{index}"
            if on_tool_start:
                on_tool_start(call_id, call.name, call.arguments)
            try:
                result = execute(call.name, call.arguments)
                if inspect.isawaitable(result):
                    result = await result
                if on_tool:
                    on_tool(call.name, call.arguments, result)
            finally:
                if on_tool_end:
                    on_tool_end(call_id)
            msgs.append(
                {"role": "tool", "tool_call_id": call.id, "content": result[:20_000]}
            )

    # MAX_ROUNDS reached: only complete if the last round actually produced
    # output (a tool-heavy run that never wrote its final answer is not a
    # deliverable either)
    return AgentResult(
        "".join(parts) if _meaningful("".join(parts)) else "",
        usage,
        MAX_ROUNDS,
        total_calls,
        complete=_meaningful("".join(parts)),
    )


def tool_log_line(name: str, arguments: str, result: str) -> str:
    """A compact, human-readable line for live logs."""
    args = arguments if len(arguments) <= 80 else arguments[:77] + "…"
    size = len(result)
    return f"\n🔧 {name}({args}) → {size} chars\n"
