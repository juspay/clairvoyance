"""Template-defined egress (HTTP functions, hooks, MCP tool calls) is SSRF-safe.

The URL is merchant-authored and may be filled from shopper-supplied vars, so
the name is resolved at request time, internal answers are refused, the
connection goes to the checked address and every redirect hop is re-checked.
"""

from __future__ import annotations

import asyncio
import socket
import time
from datetime import timedelta
from typing import Any, Dict, List

import aiohttp
import httpx
import pytest
from mcp.client.session_group import StreamableHttpParameters

import app.core.security.ssrf as ssrf

# isort: off
# template.types must load before the transport modules (circular import).
from app.ai.voice.agents.breeze_buddy.template.types import (
    HttpRequestConfig,
    McpServerConfig,
)

import app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester as hr
import app.ai.voice.agents.breeze_buddy.mcp as mcp_mod
import app.ai.voice.agents.breeze_buddy.mcp.cache as mcp_cache
import app.ai.voice.agents.breeze_buddy.mcp.egress as mcp_egress
from app.ai.voice.agents.breeze_buddy.utils.common import send_webhook_with_retry

# isort: on

PUBLIC = "93.184.216.34"


def _dns(monkeypatch, answers: Dict[str, str]) -> List[str]:
    asked: List[str] = []

    async def resolve(hostname, port):
        asked.append(hostname)
        return [answers[hostname]]

    monkeypatch.setattr(ssrf, "_resolve_host", resolve)
    return asked


class _Content:
    async def read(self, n: int) -> bytes:
        return b""


class _Response:
    def __init__(self, status: int = 200, location: str = "") -> None:
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        if location:
            self.headers["Location"] = location
        self.content = _Content()

    def release(self) -> None:
        pass


class _Session:
    def __init__(self, responses: List[_Response]) -> None:
        self._responses = list(responses)
        self.calls: List[dict] = []

    async def request(self, method, url, **kwargs) -> _Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0)


def _config(retries: int = 1) -> HttpRequestConfig:
    return HttpRequestConfig(
        url="https://api.example.com/v1/x", method="GET", max_retries=retries
    )


async def _run(session: _Session, retries: int = 1):
    executor = hr.HttpRequestExecutor(session)  # pyrefly: ignore[bad-argument-type]
    return await executor.execute(
        config=_config(retries), resolved_fields={}, fire_and_forget=False
    )


@pytest.mark.parametrize("internal", ["10.0.0.5", "169.254.169.254", "fd20:ce::254"])
async def test_name_resolving_internal_is_refused_before_any_request(
    monkeypatch, internal
):
    _dns(monkeypatch, {"api.example.com": internal})
    session = _Session([_Response()])

    assert await _run(session) == (0, "Request blocked by egress policy")
    assert session.calls == []


async def test_refusal_is_not_retried(monkeypatch):
    asked = _dns(monkeypatch, {"api.example.com": "10.0.0.5"})

    assert await _run(_Session([]), retries=3) == (
        0,
        "Request blocked by egress policy",
    )
    assert asked == ["api.example.com"]


async def test_redirect_to_internal_is_refused(monkeypatch):
    _dns(monkeypatch, {"api.example.com": PUBLIC, "internal.example": "10.1.2.3"})
    session = _Session([_Response(302, "https://internal.example/admin")])

    assert await _run(session) == (0, "Request blocked by egress policy")
    assert len(session.calls) == 1  # the internal hop is never requested


async def test_request_goes_to_checked_ip_without_auto_redirects(monkeypatch):
    _dns(monkeypatch, {"api.example.com": PUBLIC})
    session = _Session([_Response()])

    result = await _run(session)

    call = session.calls[0]
    assert result is not None and result[0] == 200
    assert call["url"] == f"https://{PUBLIC}/v1/x"
    assert call["headers"]["Host"] == "api.example.com"
    assert call["server_hostname"] == "api.example.com"
    assert call["allow_redirects"] is False


# --- MCP direct tool calls ---------------------------------------------------


class _McpResp:
    status_code = 200
    text = "{}"

    def json(self):
        return {"jsonrpc": "2.0", "result": {"content": [], "isError": False}}


class _FakeClient:
    made: List[dict] = []
    posts: List[dict] = []

    def __init__(self, **kwargs) -> None:
        _FakeClient.made.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.posts.append({"url": str(url), "headers": headers})
        return _McpResp()


def _mcp_handler(monkeypatch):
    _FakeClient.made, _FakeClient.posts = [], []
    monkeypatch.setattr(mcp_mod.httpx, "AsyncClient", _FakeClient)
    params = StreamableHttpParameters(
        url="https://shop.example.com/ai/mcp",
        headers={},
        timeout=timedelta(seconds=5),
    )
    return mcp_mod._create_direct_http_tool_handler(params, "get_order")


async def test_mcp_internal_target_refused_without_leaking_the_address(monkeypatch):
    _dns(monkeypatch, {"shop.example.com": "10.9.8.7"})
    handler = _mcp_handler(monkeypatch)

    out = await handler({}, None)

    assert out["status"] == "error"
    assert "egress policy" in out["data"] and "10.9.8.7" not in out["data"]
    assert _FakeClient.posts == []


async def test_mcp_direct_call_sends_through_the_guard(monkeypatch):
    _dns(monkeypatch, {"shop.example.com": PUBLIC})
    handler = _mcp_handler(monkeypatch)

    await handler({}, None)

    assert isinstance(_FakeClient.made[0]["transport"], mcp_egress.McpEgressTransport)


def _egress_client(monkeypatch, answer) -> tuple:
    """A real redirecting httpx client over the guard; returns (client, seen)."""
    seen: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request)

    transport = mcp_egress.McpEgressTransport()
    monkeypatch.setattr(transport, "_inner", httpx.MockTransport(handler))
    return httpx.AsyncClient(follow_redirects=True, transport=transport), seen


async def test_mcp_request_goes_to_checked_ip_keeping_the_name(monkeypatch):
    _dns(monkeypatch, {"shop.example.com": PUBLIC})
    client, seen = _egress_client(monkeypatch, lambda r: httpx.Response(200))

    await client.post("https://shop.example.com/ai/mcp")

    assert str(seen[0].url) == f"https://{PUBLIC}/ai/mcp"
    assert seen[0].headers["Host"] == "shop.example.com"
    assert seen[0].extensions["sni_hostname"] == "shop.example.com"


async def test_mcp_redirect_to_internal_is_not_followed(monkeypatch):
    _dns(monkeypatch, {"shop.example.com": PUBLIC, "internal.example": "10.1.2.3"})
    client, seen = _egress_client(
        monkeypatch,
        lambda r: httpx.Response(
            307, headers={"Location": "https://internal.example/"}
        ),
    )

    response = await client.post("https://shop.example.com/ai/mcp")

    assert response.status_code == 404
    assert len(seen) == 1  # the internal hop is never connected to


async def test_mcp_rebinding_between_hops_is_refused(monkeypatch):
    answers = iter([[PUBLIC], ["169.254.169.254"]])

    async def resolve(hostname, port):
        return next(answers)

    monkeypatch.setattr(ssrf, "_resolve_host", resolve)
    client, seen = _egress_client(
        monkeypatch, lambda r: httpx.Response(307, headers={"Location": "/next"})
    )

    response = await client.post("https://shop.example.com/ai/mcp")

    assert response.status_code == 404
    assert len(seen) == 1


async def test_mcp_relative_redirect_keeps_the_real_host(monkeypatch):
    _dns(monkeypatch, {"shop.example.com": PUBLIC})
    client, seen = _egress_client(
        monkeypatch,
        lambda r: (
            httpx.Response(200)
            if r.url.path == "/next"
            else httpx.Response(307, headers={"Location": "/next"})
        ),
    )

    await client.post("https://shop.example.com/ai/mcp")

    assert seen[1].headers["Host"] == "shop.example.com"
    assert seen[1].extensions["sni_hostname"] == "shop.example.com"


async def test_mcp_request_tries_the_next_checked_ip(monkeypatch):
    other = "93.184.216.35"

    async def two_ips(url):
        return [PUBLIC, other]

    def answer(request: httpx.Request) -> httpx.Response:
        if PUBLIC in str(request.url):
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200)

    monkeypatch.setattr(mcp_egress, "_check_mcp_egress", two_ips)
    client, seen = _egress_client(monkeypatch, answer)

    response = await client.post("https://shop.example.com/ai/mcp")

    assert response.status_code == 200
    assert [r.url.host for r in seen] == [PUBLIC, other]
    assert seen[0].extensions["timeout"]["connect"] == ssrf._ADDRESS_CONNECT_CAP
    assert seen[1].extensions["timeout"]["connect"] == 5.0  # the client's own


async def test_names_on_one_ip_do_not_share_a_connection(monkeypatch):
    _dns(monkeypatch, {"api.example.com": PUBLIC, "dash.example.com": PUBLIC})
    session: Any = _Session([_Response(), _Response(), _Response()])

    for url in ("api.example.com", "dash.example.com", "api.example.com"):
        async with ssrf.ssrf_safe_request(session, "GET", f"https://{url}/x"):
            pass

    api, dash, api_again = [call["ssl"] for call in session.calls]
    assert api is not dash  # aiohttp keys its connection pool on this
    assert api is api_again  # keep-alive still works for one host


def test_built_server_params_carry_the_guarded_client():
    params = mcp_mod._build_server_params(
        McpServerConfig(url="https://shop.example.com/ai/mcp"), {}
    )

    assert params.model_dump()["httpx_client_factory"] is (
        mcp_egress._egress_safe_client
    )


async def test_chat_discovery_refuses_internal_target(monkeypatch):
    _dns(monkeypatch, {"shop.example.com": "169.254.169.254"})

    def never_opened(server_params):
        raise AssertionError("MCPClient opened for an internal target")

    monkeypatch.setattr(mcp_cache, "MCPClient", never_opened)
    params = StreamableHttpParameters(url="https://shop.example.com/ai/mcp")

    with pytest.raises(ssrf.SSRFError, match="egress policy"):
        await mcp_cache._live_discovery(params, 5)


def _refuse_mcp_client(monkeypatch) -> List[str]:
    """Internal DNS answer, and record any MCPClient that gets opened."""
    _dns(monkeypatch, {"shop.example.com": "169.254.169.254"})
    opened: List[str] = []

    class Client:
        def __init__(self, server_params):
            opened.append(server_params.url)
            raise AssertionError("MCPClient opened for an internal target")

    monkeypatch.setattr(mcp_mod, "MCPClient", Client)
    return opened


_PARAMS = StreamableHttpParameters(url="https://shop.example.com/ai/mcp")


async def test_pooled_path_refuses_before_opening_a_client(monkeypatch):
    opened = _refuse_mcp_client(monkeypatch)

    with pytest.raises(ssrf.SSRFError, match="egress policy"):
        await mcp_mod._acquire_pooled_client({}, "key", _PARAMS)

    assert opened == []


async def test_per_call_path_refuses_before_opening_a_client(monkeypatch):
    opened = _refuse_mcp_client(monkeypatch)

    out = await mcp_mod._create_mcp_tool_handler(_PARAMS, "get_order")({}, None)

    assert out["status"] == "error" and "egress policy" in out["data"]
    assert opened == []


async def test_voice_discovery_refuses_before_opening_a_client(monkeypatch):
    opened = _refuse_mcp_client(monkeypatch)
    server = McpServerConfig(url="https://shop.example.com/ai/mcp")

    with pytest.raises(ssrf.SSRFError, match="egress policy"):
        await mcp_mod._load_server_tools(server, {}, set())

    assert opened == []


def _dns_down(monkeypatch) -> List[str]:
    asked: List[str] = []

    async def resolve(hostname, port):
        asked.append(hostname)
        raise socket.gaierror("temporary failure")

    monkeypatch.setattr(ssrf, "_resolve_host", resolve)
    return asked


async def test_http_function_retries_a_dns_failure(monkeypatch):
    asked = _dns_down(monkeypatch)

    result = await _run(_Session([]), retries=3)

    assert len(asked) == 3
    assert result != (0, "Request blocked by egress policy")


# --- reporting_webhook_url ---------------------------------------------------

_WEBHOOK = "https://hooks.example.com/outcome"


async def test_webhook_to_internal_target_is_refused_once(monkeypatch):
    asked = _dns(monkeypatch, {"hooks.example.com": "169.254.169.254"})
    session = _Session([])

    assert await send_webhook_with_retry(session, _WEBHOOK, {"a": 1}) is False
    assert session.calls == []
    assert asked == ["hooks.example.com"]  # a refusal is not retried


async def test_webhook_does_not_follow_a_redirect_to_another_host(monkeypatch):
    _dns(monkeypatch, {"hooks.example.com": PUBLIC})
    session = _Session([_Response(307, "https://elsewhere.example/x")])

    assert await send_webhook_with_retry(session, _WEBHOOK, {"a": 1}) is False
    assert len(session.calls) == 1


async def test_webhook_retries_a_dns_failure(monkeypatch):
    asked = _dns_down(monkeypatch)

    assert await send_webhook_with_retry(_Session([]), _WEBHOOK, {"a": 1}) is False
    assert len(asked) == 3


async def test_webhook_is_delivered_to_the_checked_ip(monkeypatch):
    _dns(monkeypatch, {"hooks.example.com": PUBLIC})
    session = _Session([_Response()])

    assert await send_webhook_with_retry(session, _WEBHOOK, {"a": 1}) is True
    call = session.calls[0]
    assert call["method"] == "POST" and call["json"] == {"a": 1}
    assert call["url"] == f"https://{PUBLIC}/outcome"
    assert call["headers"]["Host"] == "hooks.example.com"


# --- review follow-ups -------------------------------------------------------


async def test_a_stalled_lookup_times_out_within_the_request_budget(monkeypatch):
    def slow(host, port, *a, **k):
        time.sleep(3)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 443))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", slow)
    started = time.monotonic()
    async with aiohttp.ClientSession() as session:
        with pytest.raises(asyncio.TimeoutError):
            async with ssrf.ssrf_safe_request(
                session,
                "GET",
                "https://slow.example/",
                timeout=aiohttp.ClientTimeout(total=1),
            ):
                pass
    assert time.monotonic() - started < 2


class _DeadFirstAddress(_Session):
    """The first address never answers; the rest behave like _Session."""

    async def request(self, method, url, **kwargs) -> _Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        if len(self.calls) == 1:
            raise aiohttp.ConnectionTimeoutError("no answer")
        return self._responses.pop(0)


async def test_a_dead_address_fails_over_quickly_to_the_next(monkeypatch):
    async def two_ips(hostname, port):
        return [PUBLIC, "93.184.216.35"]

    monkeypatch.setattr(ssrf, "_resolve_host", two_ips)
    session: Any = _DeadFirstAddress([_Response()])
    timeout = aiohttp.ClientTimeout(total=10)

    async with ssrf.ssrf_safe_request(
        session, "GET", "https://api.example.com/x", timeout=timeout
    ) as response:
        assert response.status == 200

    first, second = session.calls
    assert first["timeout"].sock_connect == ssrf._ADDRESS_CONNECT_CAP
    assert second["url"] == "https://93.184.216.35/x"
    assert second["timeout"].sock_connect is None  # the last keeps the full budget


async def test_checked_answers_are_reused_for_a_while(monkeypatch):
    lookups: List[str] = []

    def lookup(host, port, *a, **k):
        lookups.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 443))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", lookup)

    for _ in range(3):
        assert await ssrf.validate_egress_url("https://cached.example/x") == [PUBLIC]

    assert lookups == ["cached.example"]


@pytest.mark.parametrize(
    "method, status, then",
    [("PUT", 301, "PUT"), ("DELETE", 302, "DELETE"), ("POST", 302, "GET")],
)
async def test_redirects_change_the_method_only_where_aiohttp_did(
    monkeypatch, method, status, then
):
    _dns(monkeypatch, {"api.example.com": PUBLIC})
    session: Any = _Session([_Response(status, "/moved"), _Response()])

    async with ssrf.ssrf_safe_request(
        session, method, "https://api.example.com/x", json={"a": 1}
    ):
        pass

    hop = session.calls[1]
    assert hop["method"] == then
    assert ("json" in hop) == (then != "GET")


async def test_too_many_redirects_keeps_its_own_message(monkeypatch):
    _dns(monkeypatch, {"api.example.com": PUBLIC})
    session = _Session([_Response(302, "/again") for _ in range(4)])

    assert await _run(session) == (0, "Too many redirects (max: 3)")


async def test_mcp_redirect_to_another_public_host_is_refused(monkeypatch):
    _dns(monkeypatch, {"shop.example.com": PUBLIC, "other.example.com": PUBLIC})
    client, seen = _egress_client(
        monkeypatch,
        lambda r: httpx.Response(
            307, headers={"Location": "https://other.example.com/"}
        ),
    )

    response = await client.post("https://shop.example.com/ai/mcp")

    assert response.status_code == 404
    assert len(seen) == 1  # the key and the tool arguments stay on the server


async def test_mcp_stalled_lookup_answers_504_within_the_budget(monkeypatch):
    async def stalled(url):
        await asyncio.sleep(3)
        return [PUBLIC]

    monkeypatch.setattr(mcp_egress, "_check_mcp_egress", stalled)
    client, seen = _egress_client(monkeypatch, lambda r: httpx.Response(200))
    client.timeout = httpx.Timeout(1.0)
    started = time.monotonic()

    response = await client.post("https://shop.example.com/ai/mcp")

    assert response.status_code == 504 and seen == []
    assert time.monotonic() - started < 2


async def test_mcp_tls_name_is_the_ascii_form_of_the_host(monkeypatch):
    _dns(monkeypatch, {"xn--fa-hia.de": PUBLIC})
    client, seen = _egress_client(monkeypatch, lambda r: httpx.Response(200))

    await client.post("https://xn--fa-hia.de/ai/mcp")

    assert seen[0].extensions["sni_hostname"] == "xn--fa-hia.de"


async def test_pipecat_hands_the_guarded_client_to_the_mcp_library(monkeypatch):
    import pipecat.services.mcp_service as pipecat_mcp

    received: Dict[str, Any] = {}

    def capture(**kwargs):
        received.update(kwargs)
        raise RuntimeError("stop before any network")

    monkeypatch.setattr(pipecat_mcp, "streamablehttp_client", capture)
    params = mcp_mod._build_server_params(
        McpServerConfig(url="https://shop.example.com/ai/mcp"), {}
    )

    with pytest.raises(RuntimeError, match="stop before any network"):
        await pipecat_mcp.MCPClient(server_params=params).start()

    assert received["httpx_client_factory"] is mcp_egress._egress_safe_client


async def test_mcp_direct_call_stalled_lookup_stays_within_the_budget(monkeypatch):
    async def stalled(url):
        await asyncio.sleep(3)
        return [PUBLIC]

    monkeypatch.setattr(mcp_mod, "_check_mcp_egress", stalled)
    _FakeClient.made, _FakeClient.posts = [], []
    monkeypatch.setattr(mcp_mod.httpx, "AsyncClient", _FakeClient)
    params = StreamableHttpParameters(
        url="https://shop.example.com/ai/mcp", timeout=timedelta(seconds=1)
    )
    handler = mcp_mod._create_direct_http_tool_handler(params, "get_order")
    started = time.monotonic()

    out = await handler({}, None)

    assert out["status"] == "error" and _FakeClient.posts == []
    assert time.monotonic() - started < 2
