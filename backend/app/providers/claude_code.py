"""Claude Max (kind `claude_code`): completions through the Claude Code CLI.

A Max subscription OAuth token (`claude setup-token`, `sk-ant-oat01-...`) is
not an API key: sent as x-api-key the API answers "credit balance too low",
because the subscription does not pay for raw API calls. Only the Claude Code
CLI may use it. So every non-code phase (spec, AI review, chat) runs as one
non-interactive `claude -p` call with no tools, the prompt on stdin and the
result read from `--output-format json`.

The token exists only in the child env dict (the same one the headless code
phase builds) and the child's memory: never in argv, never logged, and every
string that leaves this module is swept by the redactor with the token as an
extra literal.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile
from collections.abc import AsyncIterator
from typing import Any

from .. import code_graph
from .. import headless as hx
from ..registry import ResolvedModel
from .gateway import Delta, GatewayError, StreamEvent, Usage

# wall-clock bound for one completion; a spec on a big story can take minutes
CLI_TIMEOUT_SECONDS = 600.0

# what the CLI says when the subscription window is used up or the API
# answers 429 ("You've hit your limit", "Claude AI usage limit reached|<ts>",
# "API Error: 429 ... rate_limit_error")
LIMIT_MARKERS = (
    "rate_limit", "rate limit", "429", "usage limit", "hit your limit",
    "limit reached", "1302", "1308",
)

RESULT_CAP = 2000

# never available to any CLI session, whatever --tools says: nothing a session
# may write, run or fetch with (the phases that read get Read, Grep, Glob only)
DISALLOWED_TOOLS = (
    "Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "WebFetch", "WebSearch", "Task",
)


class CliTimeout(GatewayError):
    """The CLI session outlived its wall-clock bound."""


def _redact(text: str, token: str) -> str:
    return hx._redact(text, (token,))


def _flatten(messages: list[dict]) -> tuple[str | None, str]:
    """(system prompt, stdin text). One user message goes through verbatim;
    a longer history becomes a plain role-labelled transcript."""
    system = "\n\n".join(
        m["content"] for m in messages if m.get("role") == "system" and m.get("content")
    )
    turns = [m for m in messages if m.get("role") in ("user", "assistant")]
    if any(m.get("role") == "tool" for m in messages):
        raise GatewayError("claude_code provider does not support tool results")
    if len(turns) == 1 and turns[0]["role"] == "user":
        return system or None, turns[0]["content"]
    text = "\n\n".join(f"{m['role'].capitalize()}: {m['content']}" for m in turns)
    return system or None, text


async def _spawn(
    argv: list[str], *, env: dict[str, str], cwd: str, stdin: bytes, timeout: float
) -> tuple[int, bytes, bytes]:
    """Run the CLI in its own session; kill the whole group on timeout."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except TimeoutError:
        _kill(proc.pid)
        await proc.wait()
        raise
    except asyncio.CancelledError:
        _kill(proc.pid)
        raise
    return proc.returncode or 0, out, err


def _kill(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _parse(stdout: bytes) -> dict[str, Any] | None:
    """The `result` object of --output-format json (an array in some CLI
    versions: take the last result entry)."""
    try:
        data = json.loads(stdout.decode("utf-8", "replace"))
    except ValueError:
        return None
    if isinstance(data, list):
        data = next(
            (d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"),
            None,
        )
    return data if isinstance(data, dict) else None


def _fail(provider_id: str, text: str, token: str) -> GatewayError:
    """Usage/rate limits carry the words `rate limit` so the runner requeues
    with backoff exactly as for z.ai's 1113/429; anything else is a plain
    provider error."""
    text = _redact(text.strip(), token)[:RESULT_CAP]
    if any(marker in text.lower() for marker in LIMIT_MARKERS):
        return GatewayError(f"[{provider_id}] rate limit: {text}")
    return GatewayError(f"[{provider_id}] claude CLI error: {text}")


def build_cli_session(
    resolved: ResolvedModel,
    key: str | None,
    messages: list[dict],
    *,
    cli_tools: str,
    max_turns: int | None = None,
    max_output_tokens: int | None = None,
    effort: str | None = None,
    mcp_config: str | None = None,
) -> tuple[list[str], dict[str, str], str]:
    """Pure: (argv, env, prompt) of one `claude -p` session. `cli_tools` is
    the --tools value ("" = no tools); `effort` is the --effort level, no flag
    when empty; `mcp_config` is inline JSON for --mcp-config (only that server,
    its tools allowed). The env holds the Claude Code variables only (no
    PATH/HOME/CLAUDE_CONFIG_DIR: the runner of the session adds its own); the
    prompt is not in argv (stdin locally, argv after -p in a worker pod)."""
    provider = resolved.provider
    if not key:
        raise GatewayError(f"[{provider.id}] missing credentials: no OAuth token set")
    system, prompt = _flatten(messages)
    config = hx.HeadlessConfig(
        base_url=None, api_key=key, model=resolved.canonical,
        small_fast_model=resolved.canonical, oauth=True,
    )
    env = hx._claude_env(config)
    if max_output_tokens:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_output_tokens)
    argv = [
        hx.CLAUDE_BIN, "-p", "--output-format", "json",
        "--model", resolved.model_id, "--tools", cli_tools, "--no-session-persistence",
        "--disallowedTools", *DISALLOWED_TOOLS,
    ]
    if effort:
        argv += ["--effort", effort]
    if max_turns:
        argv += ["--max-turns", str(max_turns)]
    if mcp_config:
        argv += [
            "--mcp-config", mcp_config, "--strict-mcp-config",
            "--allowedTools", code_graph.ALLOWED_TOOLS,
        ]
    if system:
        argv += ["--system-prompt", system]
    return argv, env, prompt


MAX_TURNS_SUBTYPE = "error_max_turns"


def parse_cli_result(
    provider_id: str,
    key: str,
    rc: int,
    stdout: bytes | str,
    stderr: bytes | str,
    *,
    allow_turn_cap: bool = False,
) -> tuple[str, Usage]:
    """Pure: the finished session's (rc, stdout, stderr) -> (result text,
    usage), or GatewayError (limit markers map to `rate limit`); every string
    is redacted of the token. A session that hit its --max-turns cap (result
    subtype `error_max_turns`) reports it as `usage.turn_cap`; with
    `allow_turn_cap` it is returned (text possibly empty) instead of raised."""
    out = stdout.encode() if isinstance(stdout, str) else stdout
    err = stderr.encode() if isinstance(stderr, str) else stderr
    data = _parse(out)
    capped = bool(data) and data.get("subtype") == MAX_TURNS_SUBTYPE  # type: ignore[union-attr]
    if capped and allow_turn_cap:
        text = data.get("result")  # type: ignore[union-attr]
        return _redact(text, key) if isinstance(text, str) else "", Usage(
            prompt_tokens=0, completion_tokens=0, finish_reason="stop", turn_cap=True
        )
    if data is None or data.get("is_error") or rc != 0:
        text = str((data or {}).get("result") or "")
        if not text:
            text = (err or out).decode("utf-8", "replace")[-RESULT_CAP:] or f"exit code {rc}"
        raise _fail(provider_id, text, key)
    text = data.get("result")
    if not isinstance(text, str) or not text.strip():
        raise GatewayError(
            f"[{provider_id}] claude CLI returned an empty result "
            f"({data.get('subtype') or 'no subtype'})"
        )
    usage = data.get("usage") or {}
    prompt_tokens = sum(
        int(usage.get(k) or 0)
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    stop = data.get("stop_reason")
    return _redact(text, key), Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=int(usage.get("output_tokens") or 0),
        finish_reason="length" if stop in ("max_tokens", "length") else "stop",
        turn_cap=capped,
    )


async def _run_cli(
    resolved: ResolvedModel,
    key: str | None,
    messages: list[dict],
    *,
    cli_tools: str,
    cwd: str | None = None,
    max_turns: int | None = None,
    max_output_tokens: int | None = None,
    timeout: float = CLI_TIMEOUT_SECONDS,
    effort: str | None = None,
    mcp_config: str | None = None,
    allow_turn_cap: bool = False,
) -> tuple[str, Usage]:
    """One local `claude -p` session; returns (result text, usage) or raises
    GatewayError."""
    provider = resolved.provider
    argv, claude_env, prompt = build_cli_session(
        resolved, key, messages, cli_tools=cli_tools, max_turns=max_turns,
        max_output_tokens=max_output_tokens, effort=effort, mcp_config=mcp_config,
    )
    assert key  # build_cli_session raised otherwise
    env = {**hx._host_env(), **claude_env}
    # a scratch config dir (no host settings or session); the cwd is the
    # scratch dir too unless an agent phase needs the story's worktree
    workdir = tempfile.mkdtemp(prefix="fubala-claude-code-")
    env["CLAUDE_CONFIG_DIR"] = workdir
    try:
        rc, out, err = await _spawn(
            argv, env=env, cwd=cwd or workdir, stdin=prompt.encode(), timeout=timeout
        )
    except FileNotFoundError as e:
        raise GatewayError(f"[{provider.id}] claude CLI not found: {hx.CLAUDE_BIN}") from e
    except TimeoutError as e:
        raise CliTimeout(
            f"[{provider.id}] claude CLI timed out after {timeout:.0f}s"
        ) from e
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return parse_cli_result(provider.id, key, rc, out, err, allow_turn_cap=allow_turn_cap)


async def stream_claude_code(
    resolved: ResolvedModel,
    key: str | None,
    messages: list[dict],
    tools: list[dict] | None,
    max_output_tokens: int | None,
    effort: str | None = None,
) -> AsyncIterator[StreamEvent]:
    """A plain completion: no tools. Fubala's own tool loop cannot run here
    (see run_cli_agent for the phases that need to read the project)."""
    if tools:
        raise GatewayError(
            f"[{resolved.provider.id}] claude_code does not support tool calls "
            "(use run_cli_agent, or the headless executor for the code phase)"
        )
    text, usage = await _run_cli(
        resolved, key, messages, cli_tools="", max_output_tokens=max_output_tokens,
        effort=effort,
    )
    yield Delta(text)
    yield usage


# the read-only built-ins that match Fubala's list_files / read_file /
# search_files, the only tools the spec phase gets
READ_ONLY_CLI_TOOLS = "Read,Grep,Glob"


async def run_cli_agent(
    resolved: ResolvedModel,
    key: str | None,
    messages: list[dict],
    *,
    cwd: str | None,
    cli_tools: str = READ_ONLY_CLI_TOOLS,
    max_turns: int | None = None,
    max_output_tokens: int | None = None,
    timeout: float = CLI_TIMEOUT_SECONDS,
    effort: str | None = None,
    mcp_config: str | None = None,
    allow_turn_cap: bool = False,
) -> tuple[str, Usage]:
    """A tool phase (spec) as ONE Claude Code agent session in `cwd` with the
    built-in tools instead of Fubala's tool loop. The final result text is
    the phase output; limits and errors map as for a plain completion."""
    return await _run_cli(
        resolved, key, messages, cli_tools=cli_tools, cwd=cwd,
        max_turns=max_turns, max_output_tokens=max_output_tokens, timeout=timeout,
        effort=effort, mcp_config=mcp_config, allow_turn_cap=allow_turn_cap,
    )
