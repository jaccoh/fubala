"""Model gateway: one streaming interface over every provider.

All chat traffic goes through ProviderGateway.stream_chat — nothing else in the
app is allowed to know how a provider is reached. LiteLLM is an implementation
detail of LiteLLMGateway, imported lazily so startup (and tests) stay fast.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from ..registry import ProviderRegistry, ResolvedModel

# Events yielded by stream_chat, in order. Usage (when the provider reports it)
# arrives as the final event.


@dataclass(frozen=True)
class Delta:
    text: str


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    # provider's own stop reason for the round ("stop", "length",
    # "tool_calls", ...) — None when the provider never reports one.
    # "length"/"max_tokens" is how an output-cap hit becomes visible.
    finish_reason: str | None = None


@dataclass(frozen=True)
class ToolCall:
    """The model wants a tool executed before continuing. The caller runs the
    tool, appends the result as a `role: tool` message, and calls again."""

    id: str
    name: str
    arguments: str  # JSON string


StreamEvent = Delta | Usage | ToolCall


class GatewayError(Exception):
    """A provider call failed after normalization (message is user-presentable).

    The optional attributes carry the raw provider facts so the runner can
    classify on structure instead of substrings; all None for string-only
    errors (echo/fake gateways, local validation)."""

    def __init__(
        self,
        message: str = "",
        *,
        http_status: int | None = None,
        error_type: str | None = None,
        provider_code: str | None = None,
        model: str | None = None,
        prompt_tokens: int | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.error_type = error_type
        self.provider_code = provider_code
        self.model = model
        self.prompt_tokens = prompt_tokens


class StreamIdleTimeout(GatewayError):
    """The provider stream sent no chunk within the idle bound. The message
    carries "midstream" so TRANSIENT_MARKERS still requeue the story when the
    agent's round retries run out."""


def _provider_code(exc: Exception) -> str | None:
    """Best-effort provider error code from the exception body, e.g. z.ai
    `{"error": {"code": "1302"}}` or Anthropic `{"error": {"type":
    "rate_limit_error"}}`. Falls back to the exception's own `code`."""
    candidates: list = [getattr(exc, "body", None)]
    text = str(exc)
    start = text.find("{")
    if start != -1:
        try:
            candidates.append(json.loads(text[start : text.rfind("}") + 1]))
        except ValueError:
            pass
    for body in candidates:
        if not isinstance(body, dict):
            continue
        err = body.get("error", body)
        if isinstance(err, dict):
            for key in ("code", "type"):
                if err.get(key) not in (None, ""):
                    return str(err[key])
    code = getattr(exc, "code", None)
    return str(code) if code not in (None, "") else None


class ProviderGateway(Protocol):
    def stream_chat(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_output_tokens: int | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]: ...


_ECHO_REPLY = """You're talking to **echo/dev**, the built-in test provider — no API key required.

I stream a fixed markdown reply so you can exercise the whole chat pipeline
(session persistence, SSE streaming, markdown rendering) without configuring
anything. A few things worth trying:

- Switch models via the model picker (grayed-out providers need an API key in `backend/.env`)
- Ask something with a list, some `inline code`, or a block:

```python
def hello(name: str) -> str:
    return f"Hello, {name}!"
```

To talk to real models, add a key to `backend/.env` (copy from `.env.example`),
restart the server, and pick a model from that provider."""

_ECHO_AI_REVIEW = """### AI review — spec v1

- **Completeness** — the spec covers the description and maps every acceptance
  criterion to a checkable item.
- **Verifiability** — steps are ordered, small, and individually reviewable.
- **Consistency** — risks section matches the implementation steps; no
  contradictions found.
- **Feasibility** — no unstated assumptions beyond what the story establishes.

Nothing blocking; minor polish can happen during implementation.

VERDICT: APPROVED"""

_ECHO_CODE_REVIEW = """### Code review

- The diff contains exactly the file the spec asked for
- No stray changes, no placeholder content
- Coder summary matches the diff

VERDICT: APPROVED"""

_ECHO_USAGE = Usage(prompt_tokens=12, completion_tokens=150)

_ECHO_SPEC = """## Goal
Exercise the full story pipeline — queue, spec generation, human review,
approve — with zero API keys, using the built-in echo provider.

## Context
This spec was produced by `echo/dev`. It stands in for what a real model would
write: same sections, same shape, canned content. The story runner streamed it
token-by-token into the live log while the board watched.

## Implementation steps
1. Queue the story and watch it move to `in_progress` automatically
2. Read the streaming log in the story detail drawer while the spec generates
3. Verify the story lands in `review` with `spec_version: 1`
4. Approve it (human gate) and confirm it reaches `done`

## Risks & edge cases
- Rejection sends the story back to `queued` with the reason attached; the
  next spec run sees that feedback — try it
- Echo never fails; to see the `failed` column, point a story at a provider
  with a bad key

## Acceptance checks
- [ ] Story traversed backlog → queued → in_progress → review → done without
      a browser reload
- [ ] The live log showed streaming output during generation
- [ ] Approve stamped `approved_by` on the story"""


class LiteLLMGateway:
    """Routes models through LiteLLM; `echo` providers stream locally."""

    def __init__(
        self,
        registry: ProviderRegistry,
        max_output_tokens: int | None = None,
        stream_idle_timeout: float | None = None,
    ) -> None:
        self.registry = registry
        # max seconds between chunks (Settings.stream_idle_timeout_seconds);
        # None or <= 0 disables the bound
        self.stream_idle_timeout = stream_idle_timeout
        # sent as max_tokens on every litellm call (see Settings.max_output_tokens)
        self.max_output_tokens = max_output_tokens

    async def stream_chat(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_output_tokens: int | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        resolved = self.registry.resolve(model)
        if resolved.provider.kind == "echo":
            last = messages[-1]["content"] if messages else ""
            if last.startswith("=== SPEC REQUEST ==="):
                text = _ECHO_SPEC
            elif last.startswith("=== AI REVIEW REQUEST ==="):
                text = _ECHO_AI_REVIEW
            elif last.startswith("=== CODE REVIEW REQUEST ==="):
                text = _ECHO_CODE_REVIEW
            elif last.startswith("=== CODE REQUEST ==="):
                # a real tool-calling coder demo: write one file, then summarize
                if any(m.get("role") == "tool" for m in messages):
                    summary = (
                        "Implemented the spec.\n\n- wrote echo-feature.md\n"
                        "- verified: file content matches acceptance checks"
                    )
                    for word in summary.split(" "):
                        yield Delta(word + " ")
                        await asyncio.sleep(0.01)
                    yield _ECHO_USAGE
                    return
                yield ToolCall(
                    id="echo_write",
                    name="write_file",
                    arguments='{"path": "echo-feature.md", "content": '
                    '"# Executed by the echo coder\\n\\nThis file was written by a '
                    'tool-calling agent inside an isolated git worktree.\\n\\n'
                    '- [x] spec implemented\\n- [x] diff generated\\n"}',
                )
                yield _ECHO_USAGE
                return
            else:
                text = _ECHO_REPLY
            for word in text.split(" "):
                yield Delta(word + " ")
                await asyncio.sleep(0.01)
            yield _ECHO_USAGE
            return
        if resolved.provider.kind == "claude_code":
            # a Max OAuth token only works through the Claude Code CLI
            from .claude_code import stream_claude_code

            key = self.registry.effective_key(resolved.provider)
            async for event in stream_claude_code(
                resolved, key, messages, tools, max_output_tokens or self.max_output_tokens,
                effort=effort,
            ):
                yield event
            return
        async for event in self._stream_litellm(
            resolved, messages, tools, max_output_tokens=max_output_tokens
        ):
            yield event

    async def _stream_litellm(
        self,
        resolved: ResolvedModel,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        import litellm  # lazy: heavy import, not needed for echo/tests

        provider = resolved.provider
        kwargs: dict = {}
        if provider.kind == "litellm":
            lm_model = f"{provider.litellm_prefix or provider.id}/{resolved.model_id}"
        elif provider.kind == "openai_compatible":
            lm_model = f"openai/{resolved.model_id}"
            base_url = provider.base_url
            if not base_url:
                raise GatewayError(f"provider {provider.id}: missing base_url")
            kwargs["api_base"] = base_url
        else:  # pragma: no cover - guarded by registry kinds
            raise GatewayError(f"provider {provider.id}: unsupported kind {provider.kind}")

        # a custom base_url always wins, for either kind (e.g. an "openai"
        # preset pointed at a proxy); keys resolve DB-first, env fallback
        if provider.base_url:
            kwargs["api_base"] = provider.base_url
        key = self.registry.effective_key(provider)
        if key:
            kwargs["api_key"] = key

        try:
            call_kwargs = dict(kwargs)
            if tools:
                call_kwargs["tools"] = tools
            # per-call budget (the agent escalates it on a cap hit) wins; else
            # the gateway default (Settings.max_output_tokens)
            budget = max_output_tokens or self.max_output_tokens
            if budget:
                call_kwargs["max_tokens"] = budget
            idle = self.stream_idle_timeout
            if idle is not None and idle <= 0:
                idle = None

            def _stalled() -> StreamIdleTimeout:
                return StreamIdleTimeout(
                    f"[{provider.id}] MidStreamIdleTimeout: no chunk for {idle:g}s",
                    error_type="MidStreamIdleTimeout",
                    model=resolved.canonical,
                )

            try:
                response = await asyncio.wait_for(
                    litellm.acompletion(
                        model=lm_model,
                        messages=messages,
                        stream=True,
                        stream_options={"include_usage": True},
                        **call_kwargs,
                    ),
                    idle,
                )
            except TimeoutError:
                raise _stalled() from None
            iterator = response.__aiter__()
            # streamed tool calls arrive as argument fragments per index
            pending: dict[int, dict] = {}
            finish_reason: str | None = None
            while True:
                try:
                    chunk = await asyncio.wait_for(iterator.__anext__(), idle)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    aclose = getattr(response, "aclose", None)
                    if aclose is not None:
                        try:
                            await aclose()
                        except Exception:  # noqa: BLE001 - a dead stream may raise on close
                            pass
                    raise _stalled() from None
                if chunk.choices:
                    choice = chunk.choices[0]
                    delta = choice.delta
                    text = delta.content
                    if text:
                        yield Delta(text)
                    for tc in getattr(delta, "tool_calls", None) or []:
                        slot = pending.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["args"] += tc.function.arguments
                    # the LAST streamed chunk carries the provider's stop
                    # reason for the round; keep overwriting so it survives
                    reason = getattr(choice, "finish_reason", None)
                    if reason:
                        finish_reason = reason
                usage = getattr(chunk, "usage", None)
                if usage:
                    yield Usage(
                        prompt_tokens=usage.prompt_tokens or 0,
                        completion_tokens=usage.completion_tokens or 0,
                        finish_reason=finish_reason,
                    )
            for slot in sorted(pending.values(), key=lambda x: x["id"]):
                if slot["name"]:
                    yield ToolCall(
                        id=slot["id"] or f"call_{slot['name']}",
                        name=slot["name"],
                        arguments=slot["args"] or "{}",
                    )
        except GatewayError:
            raise
        except Exception as e:  # litellm raises many provider-specific types
            prompt_tokens: int | None = None
            try:
                prompt_tokens = int(litellm.token_counter(model=lm_model, messages=messages))
            except Exception:  # noqa: BLE001 - tokenizer missing/offline: None is fine
                pass
            status = getattr(e, "status_code", None)
            raise GatewayError(
                f"[{provider.id}] {type(e).__name__}: {e}",
                http_status=status if isinstance(status, int) else None,
                error_type=type(e).__name__,
                provider_code=_provider_code(e),
                model=resolved.canonical,
                prompt_tokens=prompt_tokens,
            ) from e
