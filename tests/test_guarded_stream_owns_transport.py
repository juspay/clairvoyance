"""A streaming caller gets the response, never the transport.

HttpRequestExecutor is the one caller that cannot use a result-returning
operation: it reads SSE event by event and aborts a body mid-transfer once it
crosses the size cap, so it has to hold the response open. It used to be handed
a shared aiohttp session to do that with, which put the two things that decide
whether the guard holds at all — the session and the proxy — in application
code. guarded_stream keeps the live response and takes both back.

Two guarantees, one test each: the session is built and closed inside the
network package, and the proxy applied is the deployment's rather than any a
caller might pass.
"""

from __future__ import annotations

import pytest

# isort: off
from app.ai.voice.agents.breeze_buddy.template.types import (
    HttpMethod,
    HttpRequestConfig,
)

import app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester as hr

# isort: on

import app.core.network.aiohttp_request as net_mod
import app.core.network.egress as egress_mod


class _Response:
    status = 200
    headers = {"Content-Type": "application/json"}

    class content:
        @staticmethod
        async def read(n: int) -> bytes:
            return b""

    def release(self) -> None:
        pass


class _Session:
    """Records how it was used and whether it was closed."""

    timeout = None

    def __init__(self) -> None:
        self.kwargs: dict = {}
        self.entered = False
        self.closed = False

    async def __aenter__(self) -> "_Session":
        self.entered = True
        return self

    async def __aexit__(self, *exc) -> bool:
        self.closed = True
        return False

    async def request(self, method, url, **kwargs):
        self.kwargs = kwargs
        return _Response()


@pytest.fixture
def transport(monkeypatch):
    """Stub DNS to a public address and hand back the session that gets built."""

    async def _public(hostname: str, port: int):
        return ["93.184.216.34"]

    monkeypatch.setattr(egress_mod, "_resolve_host", _public)

    made = _Session()
    monkeypatch.setattr(net_mod, "create_aiohttp_session", lambda **kw: made)
    monkeypatch.setattr(net_mod, "get_proxy_config", lambda: "http://egress:3128")
    return made


async def test_the_session_is_built_here_and_closed_here(transport):
    await hr.HttpRequestExecutor().execute(
        config=HttpRequestConfig(
            url="https://api.example.com/v1/orders",
            method=HttpMethod.GET,
            max_retries=1,
        ),
        fire_and_forget=False,
    )

    assert transport.entered, "guarded_stream never opened a session of its own"
    # A session left open outlives the request and keeps a pool that a later
    # hostname could be served out of — the reason it is per-request at all.
    assert transport.closed, "the session outlived the request"


async def test_the_deployment_proxy_is_applied_not_the_callers(transport):
    """Egress runs through the deployment's proxy where one is configured.

    A caller supplying its own would quietly change where the request goes,
    after the guard has decided the address is fine.
    """
    await hr.HttpRequestExecutor().execute(
        config=HttpRequestConfig(
            url="https://api.example.com/v1/orders",
            method=HttpMethod.GET,
            max_retries=1,
        ),
        fire_and_forget=False,
    )

    assert transport.kwargs.get("proxy") == "http://egress:3128"
