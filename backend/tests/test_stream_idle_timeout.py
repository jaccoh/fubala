"""A provider stream that goes silent fails that round, not the whole run."""

import asyncio
import logging
from types import SimpleNamespace

import litellm
import pytest
from test_gateway_max_tokens import _Chunk, _Registry, _resolved

from app.agent import MAX_STREAM_STALL_RETRIES, run_agent
from app.providers import GatewayError, StreamIdleTimeout
from app.providers.gateway import Delta, LiteLLMGateway, Usage
from app.runner import _is_transient_error


def _text(content: str):
    return _Chunk(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=None))],
        usage=None,
    )


class _Stream:
    """Yields `chunks` (sleeping `gap` before each), then blocks or ends."""

    def __init__(self, chunks, gap=0.0, block=True):
        self.chunks, self.gap, self.block = list(chunks), gap, block
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.chunks:
            await asyncio.sleep(self.gap)
            return self.chunks.pop(0)
        if self.block:
            await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def aclose(self):
        self.closed = True


def _patch(monkeypatch, stream):
    async def fake(**kwargs):
        return stream

    monkeypatch.setattr(litellm, "acompletion", fake)


async def _drain(gw):
    resolved = _resolved()
    resolved.canonical = "zai/glm-5.3"
    return [e async for e in gw._stream_litellm(resolved, [{"role": "user", "content": "hi"}])]


@pytest.mark.timeout(5)
def test_stall_after_first_chunk_raises_and_closes(monkeypatch):
    stream = _Stream([_text("a")])
    _patch(monkeypatch, stream)
    gw = LiteLLMGateway(_Registry(), stream_idle_timeout=0.2)
    with pytest.raises(StreamIdleTimeout) as ei:
        asyncio.run(_drain(gw))
    assert isinstance(ei.value, GatewayError)
    assert stream.closed
    assert _is_transient_error(str(ei.value))


@pytest.mark.timeout(5)
def test_stall_before_first_chunk_raises(monkeypatch):
    _patch(monkeypatch, _Stream([]))
    gw = LiteLLMGateway(_Registry(), stream_idle_timeout=0.2)
    with pytest.raises(StreamIdleTimeout):
        asyncio.run(_drain(gw))


@pytest.mark.timeout(5)
def test_stall_before_stream_returns_raises(monkeypatch):
    async def hang(**kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(litellm, "acompletion", hang)
    gw = LiteLLMGateway(_Registry(), stream_idle_timeout=0.2)
    with pytest.raises(StreamIdleTimeout):
        asyncio.run(_drain(gw))


@pytest.mark.timeout(5)
def test_steady_chunks_above_total_bound_complete(monkeypatch):
    chunks = [_text(str(i)) for i in range(5)]
    _patch(monkeypatch, _Stream(chunks, gap=0.1, block=False))
    gw = LiteLLMGateway(_Registry(), stream_idle_timeout=0.2)
    events = asyncio.run(_drain(gw))
    assert [e.text for e in events if isinstance(e, Delta)] == list("01234")


@pytest.mark.timeout(5)
def test_disabled_bound_does_not_time_out(monkeypatch):
    _patch(monkeypatch, _Stream([_text("a")], gap=0.3, block=False))
    gw = LiteLLMGateway(_Registry(), stream_idle_timeout=0)
    assert len(asyncio.run(_drain(gw))) == 1


class _StallingGateway:
    stream_idle_timeout = 0.2

    def __init__(self, stalls):
        self.stalls = stalls
        self.seen: list[list[dict]] = []

    async def stream_chat(self, *, model, messages, tools=None, max_output_tokens=None, **kw):
        self.seen.append([dict(m) for m in messages])
        if len(self.seen) <= self.stalls:
            yield Delta("partial ")
            raise StreamIdleTimeout("[p] MidStreamIdleTimeout: no chunk for 0.2s")
        yield Delta("done")
        yield Usage(prompt_tokens=1, completion_tokens=1, finish_reason="stop")


@pytest.mark.anyio
async def test_agent_retries_a_stalled_round(caplog):
    gw = _StallingGateway(stalls=1)
    with caplog.at_level(logging.WARNING, logger="app.agent"):
        result = await run_agent(
            gw,
            model="m/x",
            messages=[{"role": "user", "content": "go"}],
            log_context="story-42",
        )
    assert result.text.endswith("done")
    assert len(gw.seen) == 2 and gw.seen[0] == gw.seen[1]
    line = next(r.getMessage() for r in caplog.records if "stream idle" in r.getMessage())
    assert "story=story-42" in line and "model=m/x" in line
    assert "round=1" in line and "idle=0.2s" in line and "(1/3)" in line


@pytest.mark.anyio
async def test_agent_reraises_after_repeated_stalls():
    gw = _StallingGateway(stalls=MAX_STREAM_STALL_RETRIES + 1)
    with pytest.raises(StreamIdleTimeout) as ei:
        await run_agent(gw, model="m/x", messages=[{"role": "user", "content": "go"}])
    assert len(gw.seen) == MAX_STREAM_STALL_RETRIES + 1
    assert _is_transient_error(str(ei.value))
