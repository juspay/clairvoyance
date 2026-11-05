"""Bedrock: every reply stream is closed — interrupted replies included."""

from __future__ import annotations

import asyncio

from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.aws.llm import AWSBedrockLLMService

from app.ai.voice.llm import BedrockConfig, build_bedrock_llm


class _Stream:
    """A reply stream; ``events=None`` streams until cancelled."""

    def __init__(self, events: int | None) -> None:
        self.closed = False
        self._events = events

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        n = 0
        while self._events is None or n < self._events:
            n += 1
            yield {"contentBlockDelta": {"delta": {"text": "x"}}}
            await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True


class _Client:
    def __init__(self, events: int | None) -> None:
        self.streams: list[_Stream] = []
        self._events = events

    async def converse_stream(self, **params):
        stream = _Stream(self._events)
        self.streams.append(stream)
        return {"stream": stream}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, events: int | None) -> None:
        self.client_obj = _Client(events)

    def client(self, service_name, **kwargs):
        return self.client_obj


def _service(events: int | None):
    svc = build_bedrock_llm(
        BedrockConfig(model="m", region="ap-south-1", max_tokens=16)
    )
    backend = _Session(events)
    svc._aws_session = backend  # type: ignore[assignment]

    async def _drop(*args, **kwargs):  # no downstream pipeline in a unit test
        pass

    svc.push_frame = _drop
    return svc, backend.client_obj


def _context() -> LLMContext:
    return LLMContext(messages=[{"role": "user", "content": "hello"}])


async def test_interrupted_reply_closes_its_stream():
    svc, client = _service(events=None)  # the reply would stream forever

    task = asyncio.create_task(svc._process_context(_context()))
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()  # the caller spoke over the bot
    try:
        await task
    except asyncio.CancelledError:
        pass

    (stream,) = client.streams
    assert stream.closed


async def test_finished_reply_closes_its_stream_too():
    svc, client = _service(events=2)

    await svc._process_context(_context())

    assert client.streams[0].closed


def test_builder_returns_pipecats_service():
    svc = build_bedrock_llm(
        BedrockConfig(model="m", region="ap-south-1", max_tokens=10)
    )
    assert isinstance(svc, AWSBedrockLLMService)
