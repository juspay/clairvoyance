"""``HttpRequestExecutor`` reads the whole response body, not one chunk.

A single ``StreamReader.read(n)`` can return a PARTIAL body on slow or
chunked upstreams (observed: 3KB of a 20KB long-poll response, which
truncated the JSON the LLM then saw). The executor must accumulate until
EOF while still enforcing ``HTTP_REQUEST_MAX_RESPONSE_BYTES``.
"""

from __future__ import annotations

import json
from typing import List

# isort: off
# template.types must load before the transport modules (reversing the
# order trips a circular import — see test_response_transform.py).
from app.ai.voice.agents.breeze_buddy.template.types import HttpRequestConfig

import app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester as hr

# isort: on


class _Content:
    """Hands back one queued chunk per read(), then EOF — the shape of a
    chunked upstream where read(n) never returns the full body."""

    def __init__(self, chunks: List[bytes]) -> None:
        self._chunks = list(chunks)
        self.reads = 0

    async def read(self, n: int) -> bytes:
        self.reads += 1
        return self._chunks.pop(0) if self._chunks else b""


class _Response:
    def __init__(self, chunks: List[bytes], status: int = 200) -> None:
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self.content = _Content(chunks)

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _Session:
    def __init__(self, chunks: List[bytes]) -> None:
        self.response = _Response(chunks)

    def request(self, **kwargs) -> _Response:
        return self.response


_CFG = HttpRequestConfig(
    url="https://api.example.com/v1/journeys", method="GET", max_retries=1
)


async def test_body_is_accumulated_until_eof():
    session = _Session([b'{"journeys": [', b'{"id": 1}, ', b'{"id": 2}]}'])
    executor = hr.HttpRequestExecutor(session)  # pyrefly: ignore[bad-argument-type]

    result = await executor.execute(
        config=_CFG, resolved_fields={}, fire_and_forget=False
    )
    assert result is not None
    status, body = result

    assert status == 200
    assert json.loads(body) == {"journeys": [{"id": 1}, {"id": 2}]}
    assert session.response.content.reads == 4  # three chunks + EOF


async def test_size_cap_counts_every_chunk(monkeypatch):
    monkeypatch.setattr(hr, "HTTP_REQUEST_MAX_RESPONSE_BYTES", 10)
    session = _Session([b"x" * 8, b"y" * 8, b"z" * 8])
    executor = hr.HttpRequestExecutor(session)  # pyrefly: ignore[bad-argument-type]

    result = await executor.execute(
        config=_CFG, resolved_fields={}, fire_and_forget=False
    )
    assert result is not None
    status, body = result

    assert status == 0
    assert "exceeded max size of 10 bytes" in body
    # stops reading once the cap is crossed — never buffers the whole body
    assert session.response.content.reads == 2
