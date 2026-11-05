"""Claude Max (Claude Code OAuth) provider: kind `claude_code`.

A Max subscription token (`sk-ant-oat01-...`) cannot be used as an API key,
so the provider runs entirely through the Claude Code CLI: the code phase
gets CLAUDE_CODE_OAUTH_TOKEN in its child env, and every other phase (spec,
AI review, chat) is a non-interactive `claude -p` completion. No real CLI and
no real token is used here: the subprocess is mocked or a fake `claude`.
"""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from app import headless as hx
from app.headless import HeadlessConfig
from app.presets import PRESETS
from app.providers import claude_code as cc
from app.providers.gateway import Delta, GatewayError, LiteLLMGateway, Usage

TOKEN = "sk-ant-oat01-test-token-do-not-leak-0123456789"


def _provider(kind: str = "claude_code"):
    return SimpleNamespace(
        id="claude-max", kind=kind, litellm_prefix=None, base_url=None,
        api_key=TOKEN, env_key=None,
    )


def _resolved(kind: str = "claude_code"):
    return SimpleNamespace(
        provider=_provider(kind), model_id="claude-sonnet-5-5", skill="medium",
        cost="medium", canonical="claude-max/claude-sonnet-5-5",
    )


class _Registry:
    def __init__(self, kind: str = "claude_code"):
        self.kind = kind

    def resolve(self, canonical):
        return _resolved(self.kind)

    def effective_key(self, provider):
        return provider.api_key


def _ok_json(text: str = "hello", **extra) -> bytes:
    body = {
        "type": "result", "subtype": "success", "is_error": False, "result": text,
        "usage": {"input_tokens": 7, "output_tokens": 3,
                  "cache_read_input_tokens": 5, "cache_creation_input_tokens": 0},
    }
    body.update(extra)
    return json.dumps(body).encode()


def _mock_spawn(monkeypatch, *, rc: int = 0, out: bytes = b"", err: bytes = b""):
    seen: dict = {}

    async def fake_spawn(argv, *, env, cwd, stdin, timeout):
        seen.update(argv=argv, env=env, cwd=cwd, stdin=stdin, timeout=timeout)
        return rc, out, err

    monkeypatch.setattr(cc, "_spawn", fake_spawn)
    return seen


async def _collect(gw, messages, **kw):
    return [e async for e in gw.stream_chat(model="claude-max/claude-sonnet-5-5",
                                            messages=messages, **kw)]


def _run(messages, **kw):
    return asyncio.run(_collect(LiteLLMGateway(_Registry()), messages, **kw))


# ---- preset ----------------------------------------------------------------


def test_preset_registers_the_kind_and_static_models():
    preset = PRESETS["claude-max"]
    assert preset["kind"] == "claude_code"
    assert preset["display_name"] == "Claude Max (Claude Code OAuth)"
    assert preset["base_url"] is None
    assert preset["env_key"] == "CLAUDE_CODE_OAUTH_TOKEN"
    models = {m["model_id"]: m["skill"] for m in preset["models"]}
    assert models == {
        "claude-opus-5-5": "high", "claude-sonnet-5-5": "medium", "claude-haiku-5-5": "low",
    }


def test_create_from_preset_and_key_hint_only(client):
    # a fresh DB seeds the preset; an existing one gets it created by hand
    res = client.post("/api/v1/providers", json={"preset": "claude-max", "id": "max2"})
    assert res.status_code == 201, res.text
    assert res.json()["kind"] == "claude_code"
    res = client.post("/api/v1/providers/claude-max/key", json={"key": TOKEN})
    body = res.json()
    assert body["has_key"] is True
    assert body["key_hint"] == TOKEN[-4:]
    assert TOKEN not in json.dumps(client.get("/api/v1/providers").json())


def test_refresh_models_has_no_remote_list(client):
    res = client.post("/api/v1/providers/claude-max/refresh_models", json={})
    assert res.status_code == 400
    assert "manually" in res.json()["detail"]


def test_kind_can_be_patched_to_claude_code(client):
    res = client.patch("/api/v1/providers/custom", json={"kind": "claude_code"})
    assert res.status_code == 200
    assert res.json()["kind"] == "claude_code"


# ---- (a) code phase: child env ---------------------------------------------


def test_oauth_child_env_uses_the_oauth_token_only():
    config = HeadlessConfig(
        base_url=None, api_key=TOKEN, model="claude-max/claude-sonnet-5-5",
        small_fast_model="claude-max/claude-haiku-5-5", oauth=True,
    )
    env = hx._child_env(config)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["ANTHROPIC_MODEL"] == "claude-sonnet-5-5"
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "claude-haiku-5-5"
    assert env["DISABLE_TELEMETRY"] == "1"


def test_oauth_never_sends_a_base_url_even_if_one_is_set():
    config = HeadlessConfig(
        base_url="https://api.z.ai/api/anthropic", api_key=TOKEN,
        model="claude-max/claude-sonnet-5-5", oauth=True,
    )
    assert "ANTHROPIC_BASE_URL" not in hx._child_env(config)


def test_api_key_child_env_is_unchanged():
    config = HeadlessConfig(
        base_url="https://api.z.ai/api/anthropic", api_key="sk-zai-x",
        model="zai/glm-5.3",
    )
    env = hx._child_env(config)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-zai-x"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


# ---- (b) other phases: the CLI completion ----------------------------------


def test_success_streams_text_and_usage(monkeypatch):
    seen = _mock_spawn(monkeypatch, out=_ok_json("the spec"))
    events = _run([
        {"role": "system", "content": "You write specs."},
        {"role": "user", "content": "=== SPEC REQUEST ===\nmake it"},
    ])
    assert events[0] == Delta("the spec")
    assert events[1] == Usage(prompt_tokens=12, completion_tokens=3, finish_reason="stop")
    argv = seen["argv"]
    assert argv[:2] == [hx.CLAUDE_BIN, "-p"]
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--system-prompt") + 1] == "You write specs."
    # the prompt travels on stdin, never in argv
    assert seen["stdin"] == b"=== SPEC REQUEST ===\nmake it"
    assert "=== SPEC REQUEST ===" not in " ".join(argv)


def test_completion_env_is_the_oauth_env(monkeypatch):
    seen = _mock_spawn(monkeypatch, out=_ok_json())
    _run([{"role": "user", "content": "hi"}], max_output_tokens=4096)
    env = seen["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert "ANTHROPIC_AUTH_TOKEN" not in env and "ANTHROPIC_BASE_URL" not in env
    assert env["ANTHROPIC_MODEL"] == "claude-sonnet-5-5"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "4096"
    assert "FUBALA_DATABASE_PATH" not in env


def test_no_system_prompt_flag_without_a_system_message(monkeypatch):
    seen = _mock_spawn(monkeypatch, out=_ok_json())
    _run([{"role": "user", "content": "hi"}])
    assert "--system-prompt" not in seen["argv"]


def test_multi_turn_history_is_flattened_onto_stdin(monkeypatch):
    seen = _mock_spawn(monkeypatch, out=_ok_json())
    _run([
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ])
    text = seen["stdin"].decode()
    assert text.index("one") < text.index("two") < text.index("three")


def test_tools_are_not_supported_on_this_provider(monkeypatch):
    _mock_spawn(monkeypatch, out=_ok_json())
    with pytest.raises(GatewayError, match="tool"):
        _run([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])


def test_max_tokens_stop_reason_maps_to_length(monkeypatch):
    _mock_spawn(monkeypatch, out=_ok_json(stop_reason="max_tokens"))
    events = _run([{"role": "user", "content": "hi"}])
    assert events[-1].finish_reason == "length"


@pytest.mark.parametrize(
    "text",
    [
        "API Error: 429 rate_limit_error",
        "You've hit your limit · resets 3pm",
        "Claude AI usage limit reached|1761700000",
        "5-hour limit reached",
    ],
)
def test_usage_limit_maps_to_the_rate_limit_path(monkeypatch, text):
    from app.runner import _is_infra_error, _is_rate_limit_error

    out = json.dumps({"type": "result", "is_error": True, "result": text}).encode()
    _mock_spawn(monkeypatch, rc=1, out=out)
    with pytest.raises(GatewayError) as exc:
        _run([{"role": "user", "content": "hi"}])
    assert _is_rate_limit_error(str(exc.value))
    # a rate limit must never read as an infrastructure failure
    assert not (_is_infra_error(str(exc.value)) and not _is_rate_limit_error(str(exc.value)))


def test_other_failure_is_a_plain_provider_error(monkeypatch):
    from app.runner import _is_rate_limit_error

    out = json.dumps({"type": "result", "is_error": True, "result": "model exploded"}).encode()
    _mock_spawn(monkeypatch, rc=1, out=out)
    with pytest.raises(GatewayError) as exc:
        _run([{"role": "user", "content": "hi"}])
    assert "model exploded" in str(exc.value)
    assert "[claude-max]" in str(exc.value)
    assert not _is_rate_limit_error(str(exc.value))


def test_non_json_output_and_stderr_is_an_error(monkeypatch):
    _mock_spawn(monkeypatch, rc=2, out=b"garbage", err=b"boom on stderr")
    with pytest.raises(GatewayError, match="boom on stderr"):
        _run([{"role": "user", "content": "hi"}])


def test_rate_limit_on_stderr_with_empty_stdout(monkeypatch):
    from app.runner import _is_rate_limit_error

    _mock_spawn(monkeypatch, rc=1, out=b"", err=b"API Error: 429 too many")
    with pytest.raises(GatewayError) as exc:
        _run([{"role": "user", "content": "hi"}])
    assert _is_rate_limit_error(str(exc.value))


def test_empty_result_text_is_an_error(monkeypatch):
    _mock_spawn(monkeypatch, out=_ok_json(""))
    with pytest.raises(GatewayError, match="empty"):
        _run([{"role": "user", "content": "hi"}])


def test_missing_binary_is_a_provider_error(monkeypatch):
    monkeypatch.setattr(cc.hx, "CLAUDE_BIN", "claude-does-not-exist-xyz")
    with pytest.raises(GatewayError, match="not found"):
        _run([{"role": "user", "content": "hi"}])


def test_real_subprocess_roundtrip_with_a_fake_claude(tmp_path, monkeypatch):
    """The real _spawn: stdin reaches the child, the env is the closed one."""
    script = tmp_path / "claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "prompt = sys.stdin.read()\n"
        "seen = sorted(k for k in os.environ if k.startswith(('ANTHROPIC', 'CLAUDE_CODE_OAUTH')))\n"
        "print(json.dumps({'type': 'result', 'is_error': False,\n"
        "  'result': prompt + '|' + ','.join(seen), 'usage': {}}))\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    events = _run([{"role": "user", "content": "ping"}])
    assert events[0].text.startswith("ping|")
    assert "CLAUDE_CODE_OAUTH_TOKEN" in events[0].text
    assert "ANTHROPIC_AUTH_TOKEN" not in events[0].text


def test_other_kinds_still_take_the_litellm_path(monkeypatch):
    """litellm / openai_compatible never touch the CLI."""
    _mock_spawn(monkeypatch, out=_ok_json())
    called = []

    async def boom(*a, **k):
        called.append(1)
        raise RuntimeError("litellm path reached")

    import litellm

    monkeypatch.setattr(litellm, "acompletion", boom)
    gw = LiteLLMGateway(_Registry("litellm"))
    with pytest.raises(GatewayError, match="litellm path reached"):
        asyncio.run(_collect(gw, [{"role": "user", "content": "hi"}]))
    assert called


# ---- (c) the token never leaks ---------------------------------------------


def test_token_echoed_back_is_redacted_from_results_and_errors(monkeypatch):
    _mock_spawn(monkeypatch, out=_ok_json(f"here is {TOKEN} ok"))
    events = _run([{"role": "user", "content": "hi"}])
    assert TOKEN not in events[0].text

    out = json.dumps({"type": "result", "is_error": True, "result": f"bad {TOKEN}"}).encode()
    _mock_spawn(monkeypatch, rc=1, out=out, err=f"stderr {TOKEN}".encode())
    with pytest.raises(GatewayError) as exc:
        _run([{"role": "user", "content": "hi"}])
    assert TOKEN not in str(exc.value)


def test_unusual_token_shape_is_redacted_by_literal(monkeypatch):
    """A token the generic patterns do not know is swept by exact match."""
    odd = "opaque-oauth-credential-value"
    registry = _Registry()
    registry.effective_key = lambda provider: odd
    _mock_spawn(monkeypatch, out=_ok_json(f"leak {odd}"))
    events = asyncio.run(_collect(LiteLLMGateway(registry), [{"role": "user", "content": "hi"}]))
    assert odd not in events[0].text


def test_token_never_reaches_argv_or_logs(monkeypatch, caplog):
    seen = _mock_spawn(monkeypatch, out=_ok_json())
    with caplog.at_level(logging.DEBUG):
        _run([{"role": "user", "content": "hi"}])
    assert TOKEN not in " ".join(seen["argv"])
    assert TOKEN not in caplog.text
    assert TOKEN not in seen["stdin"].decode()


# ---- --effort per phase ----------------------------------------------------


def _flag_value(argv, flag):
    return argv[argv.index(flag) + 1]


def test_completion_passes_effort_to_the_cli(monkeypatch):
    seen = _mock_spawn(monkeypatch, out=_ok_json())
    _run([{"role": "user", "content": "hi"}], effort="high")
    assert _flag_value(seen["argv"], "--effort") == "high"


@pytest.mark.parametrize("effort", [None, ""])
def test_no_effort_means_no_flag(monkeypatch, effort):
    seen = _mock_spawn(monkeypatch, out=_ok_json())
    _run([{"role": "user", "content": "hi"}], effort=effort)
    assert "--effort" not in seen["argv"]


def test_effort_kwarg_is_optional_for_the_gateway(monkeypatch):
    seen = _mock_spawn(monkeypatch, out=_ok_json())
    _run([{"role": "user", "content": "hi"}])
    assert "--effort" not in seen["argv"]


def test_cli_agent_session_passes_effort_and_keeps_its_other_flags(monkeypatch):
    seen = _mock_spawn(monkeypatch, out=_ok_json("spec"))
    text, _ = asyncio.run(
        cc.run_cli_agent(
            _resolved(), TOKEN, [{"role": "user", "content": "spec it"}],
            cwd="/tmp", effort="medium", max_turns=5,
        )
    )
    assert text == "spec"
    argv = seen["argv"]
    assert _flag_value(argv, "--effort") == "medium"
    assert _flag_value(argv, "--tools") == "Read,Grep,Glob"
    assert "--max-turns" in argv


def test_non_claude_code_kinds_never_see_effort(monkeypatch):
    seen = _mock_spawn(monkeypatch, out=_ok_json())
    kwargs: dict = {}

    async def boom(*a, **k):
        kwargs.update(k)
        raise RuntimeError("litellm path reached")

    import litellm

    monkeypatch.setattr(litellm, "acompletion", boom)
    gw = LiteLLMGateway(_Registry("litellm"))
    with pytest.raises(GatewayError, match="litellm path reached"):
        asyncio.run(_collect(gw, [{"role": "user", "content": "hi"}], effort="high"))
    assert "effort" not in kwargs
    assert "argv" not in seen  # the CLI was never spawned


def test_headless_argv_carries_effort_for_oauth_only():
    def argv(**kw):
        config = HeadlessConfig(
            base_url=None, api_key=TOKEN, model="claude-max/claude-sonnet-5-5", **kw
        )
        return hx.child_argv("go", config)

    assert _flag_value(argv(oauth=True, effort="medium"), "--effort") == "medium"
    assert "--effort" not in argv(oauth=True, effort="")
    assert "--effort" not in argv(oauth=True)
    # an API-key provider's argv is unchanged even if an effort leaks in
    assert "--effort" not in argv(oauth=False, effort="medium")
