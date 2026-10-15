"""``HttpRequestExecutor`` reads the whole response body, not one chunk.

A single ``StreamReader.read(n)`` can return a PARTIAL body on slow or
chunked upstreams (observed: 3KB of a 20KB long-poll response, which
truncated the JSON the LLM then saw). The executor must accumulate until
EOF while still enforcing ``HTTP_REQUEST_MAX_RESPONSE_BYTES``.
"""

from __future__ import annotations

import json
from typing import List

import pytest

# isort: off
# template.types must load before the transport modules (reversing the
# order trips a circular import — see test_response_transform.py).
from app.ai.voice.agents.breeze_buddy.template.types import HttpRequestConfig

import app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester as hr

# isort: on

import app.core.network.aiohttp_request as net_mod
import app.core.network.egress as ssrf_mod


@pytest.fixture(autouse=True)
def _stub_egress_resolution(monkeypatch):
    """The egress guard resolves the host before the request goes out.

    These tests drive a fake session to pin how the BODY is read, and must not
    depend on DNS or the network to do it. Resolution is stubbed to a public
    address; what the guard does with the answer is covered in
    tests/test_ssrf_egress.py.
    """

    async def _public(hostname: str, port: int):
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_mod, "_resolve_host", _public)


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

    def release(self) -> None:
        # ssrf_safe_request releases each response it drives.
        pass


class _Session:
    """Stands in for the session guarded_stream builds for itself.

    The executor no longer holds a session, so the seam moved into the network
    package: create_aiohttp_session is what gets replaced, and the double has
    to be an async context manager because that is how guarded_stream uses it.
    """

    timeout = None

    def __init__(self, chunks: List[bytes]) -> None:
        self.response = _Response(chunks)

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def request(self, *args, **kwargs) -> _Response:
        # ssrf_safe_request AWAITS session.request(method, url, ...) — method
        # and url positional — rather than using it as an async context manager.
        return self.response


@pytest.fixture
def session(monkeypatch):
    """Install a chunked-response session and hand it back for inspection."""

    def _install(chunks: List[bytes]) -> _Session:
        made = _Session(chunks)
        monkeypatch.setattr(net_mod, "create_aiohttp_session", lambda **kw: made)
        return made

    return _install


_CFG = HttpRequestConfig(
    url="https://api.example.com/v1/journeys", method="GET", max_retries=1
)


async def test_body_is_accumulated_until_eof(session):
    session = session([b'{"journeys": [', b'{"id": 1}, ', b'{"id": 2}]}'])
    executor = hr.HttpRequestExecutor()

    result = await executor.execute(
        config=_CFG, resolved_fields={}, fire_and_forget=False
    )
    assert result is not None
    status, body = result

    assert status == 200
    assert json.loads(body) == {"journeys": [{"id": 1}, {"id": 2}]}
    assert session.response.content.reads == 4  # three chunks + EOF


async def test_size_cap_counts_every_chunk(monkeypatch, session):
    monkeypatch.setattr(hr, "HTTP_REQUEST_MAX_RESPONSE_BYTES", 10)
    session = session([b"x" * 8, b"y" * 8, b"z" * 8])
    executor = hr.HttpRequestExecutor()

    result = await executor.execute(
        config=_CFG, resolved_fields={}, fire_and_forget=False
    )
    assert result is not None
    status, body = result

    assert status == 0
    assert "exceeded max size of 10 bytes" in body
    # stops reading once the cap is crossed — never buffers the whole body
    assert session.response.content.reads == 2
