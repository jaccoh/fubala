"""The gateway must send an explicit output budget (max_tokens).

Without it, litellm's anthropic adapter falls back to
DEFAULT_ANTHROPIC_CHAT_MAX_TOKENS (4096) for models it does not know
(e.g. zai's glm-5.3 via the anthropic endpoint). GLM then spends the whole
budget on thinking and returns no text and no tool calls: empty specs and
"coder produced no changes" (16 Oct, stories 23c0de95/e8396a7e).
"""

import asyncio
from types import SimpleNamespace

import litellm
import pytest

from app.config import Settings
from app.providers.gateway import LiteLLMGateway


class _Chunk(SimpleNamespace):
    pass


def _resolved(kind: str = "litellm"):
    provider = SimpleNamespace(
        id="zai", kind=kind, litellm_prefix="anthropic", base_url="https://example.invalid",
        api_key="k", env_key=None,
    )
    return SimpleNamespace(provider=provider, model_id="glm-5.3", skill="high", cost="medium")


class _Registry:
    def effective_key(self, provider):
        return provider.api_key


def _capture(monkeypatch) -> dict:
    seen: dict = {}

    async def fake_acompletion(**kwargs):
        seen.update(kwargs)

        async def gen():
            yield _Chunk(choices=[SimpleNamespace(delta=SimpleNamespace(content="ok", tool_calls=None))], usage=None)
            yield _Chunk(choices=[], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

        return gen()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    return seen


async def _drain(gw: LiteLLMGateway) -> None:
    async for _ in gw._stream_litellm(_resolved(), [{"role": "user", "content": "hi"}]):
        pass


def test_default_output_budget_is_sent(monkeypatch):
    seen = _capture(monkeypatch)
    gw = LiteLLMGateway(_Registry(), max_output_tokens=Settings().max_output_tokens)
    asyncio.run(_drain(gw))
    assert seen["max_tokens"] == 16384


def test_configured_output_budget_is_sent(monkeypatch):
    seen = _capture(monkeypatch)
    gw = LiteLLMGateway(_Registry(), max_output_tokens=32000)
    asyncio.run(_drain(gw))
    assert seen["max_tokens"] == 32000


def test_no_budget_when_disabled(monkeypatch):
    seen = _capture(monkeypatch)
    gw = LiteLLMGateway(_Registry(), max_output_tokens=None)
    asyncio.run(_drain(gw))
    assert "max_tokens" not in seen


def test_setting_reads_env(monkeypatch):
    monkeypatch.setenv("FUBALA_MAX_OUTPUT_TOKENS", "24000")
    assert Settings().max_output_tokens == 24000


@pytest.mark.parametrize("kind", ["litellm", "openai_compatible"])
def test_both_provider_kinds_get_the_budget(monkeypatch, kind):
    seen = _capture(monkeypatch)
    gw = LiteLLMGateway(_Registry(), max_output_tokens=16384)

    async def run():
        async for _ in gw._stream_litellm(_resolved(kind), [{"role": "user", "content": "hi"}]):
            pass

    asyncio.run(run())
    assert seen["max_tokens"] == 16384
