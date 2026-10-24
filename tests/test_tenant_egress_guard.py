"""Template-defined egress (HTTP functions, hooks, MCP tool calls) is SSRF-safe.

The URL is merchant-authored and may be filled from shopper-supplied vars, so
the name is resolved at request time, internal answers are refused, the
connection goes to the checked address and every redirect hop is re-checked.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Dict, List

import pytest
from mcp.client.session_group import StreamableHttpParameters

import app.core.security.ssrf as ssrf

# isort: off
# template.types must load before the transport modules (circular import).
from app.ai.voice.agents.breeze_buddy.template.types import HttpRequestConfig

import app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester as hr
import app.ai.voice.agents.breeze_buddy.mcp as mcp_mod

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

    async def post(self, url, json=None, headers=None, extensions=None):
        _FakeClient.posts.append(
            {"url": str(url), "headers": headers, "extensions": extensions}
        )
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


async def test_mcp_direct_call_is_pinned_to_the_checked_ip(monkeypatch):
    _dns(monkeypatch, {"shop.example.com": PUBLIC})
    handler = _mcp_handler(monkeypatch)

    await handler({}, None)

    post = _FakeClient.posts[0]
    assert post["url"] == f"https://{PUBLIC}/ai/mcp"
    assert post["headers"]["Host"] == "shop.example.com"
    assert post["extensions"] == {"sni_hostname": "shop.example.com"}
