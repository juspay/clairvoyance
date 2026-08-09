"""End-to-end proof that the SSRF egress guard blocks before a packet leaves.

Runs against a REAL loopback HTTP server, asserting on that server's request
ledger — the only way to show a blocked destination was never contacted, which
a mocked transport cannot demonstrate.

Skipped until the SSRF egress guard lands (PR #987). Once both are on
``release`` this runs alongside the telephony suite from the same directory.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterator, List, Tuple, cast

import pytest

ssrf = pytest.importorskip(
    "app.core.security.ssrf",
    reason="SSRF egress guard not on this branch yet (arrives with PR #987)",
)


class _RecordingServer(HTTPServer):
    """Owns its own request ledger, so no state is shared between tests."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hits: List[str] = []


class _Recorder(BaseHTTPRequestHandler):
    def do_GET(self):
        cast(_RecordingServer, self.server).hits.append(f"{self.command} {self.path}")
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, format: str, *args: Any) -> None:
        """Silence stderr access logging; signature matches the base class."""


@pytest.fixture
def recorder() -> Iterator[Tuple[str, List[str]]]:
    srv = _RecordingServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", srv.hits
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize(
    "ip,label",
    [
        ("127.0.0.1", "loopback"),
        ("169.254.169.254", "cloud metadata"),
        ("10.0.0.5", "RFC1918"),
        ("192.168.1.1", "RFC1918"),
        ("::1", "IPv6 loopback"),
        ("::ffff:169.254.169.254", "IPv4-mapped metadata"),
    ],
)
def test_non_public_addresses_are_blocked(ip, label):
    assert ssrf.ip_block_reason(ip) is not None, f"{label} {ip} must be blocked"


def test_loopback_url_is_rejected_over_real_dns(recorder):
    base, _ = recorder
    with pytest.raises(ssrf.SSRFError):
        asyncio.run(ssrf.validate_egress_url(base + "/x", allow_http=True))


def test_hostname_resolving_to_loopback_is_rejected(recorder):
    base, _ = recorder
    port = base.rsplit(":", 1)[1]
    with pytest.raises(ssrf.SSRFError):
        asyncio.run(
            ssrf.validate_egress_url(f"http://localhost:{port}/x", allow_http=True)
        )


def test_cloud_metadata_is_rejected():
    with pytest.raises(ssrf.SSRFError):
        asyncio.run(
            ssrf.validate_egress_url(
                "http://169.254.169.254/latest/meta-data/", allow_http=True
            )
        )


def test_plain_http_is_rejected_unless_explicitly_allowed():
    with pytest.raises(ssrf.SSRFError):
        asyncio.run(ssrf.validate_egress_url("http://example.com/"))


def test_blocked_request_never_reaches_the_socket(recorder):
    """The assertion that matters: zero bytes reached the destination."""
    base, hits = recorder

    async def go():
        import aiohttp

        async with aiohttp.ClientSession() as s:
            async with ssrf.ssrf_safe_request(
                s, "GET", base + "/probe", allow_http=True
            ):
                pass

    with pytest.raises(ssrf.SSRFError):
        asyncio.run(go())
    assert hits == [], "guard must block before any connection is made"


def test_mcp_precheck_rejects_a_private_server_without_contacting_it(recorder):
    """Covers the pre-check MCP path, which validates via ``_build_server_params``
    before a credential header is ever assembled."""
    base, hits = recorder
    port = base.rsplit(":", 1)[1]

    from app.ai.voice.agents.breeze_buddy.handlers.transport.http_requester import (
        HttpRequestExecutor,
    )
    from app.ai.voice.agents.breeze_buddy.managers.pre_checks.http import (
        _fetch_mcp_response,
    )
    from app.ai.voice.agents.breeze_buddy.template.types import McpServerConfig
    from app.schemas import PreCheckConfig

    # https:// so the scheme gate passes and the IP policy is what decides.
    server = McpServerConfig(
        name="e2e-sim",
        url=f"https://127.0.0.1:{port}/mcp",
        headers={"X-Tenant-Secret": "must-never-egress"},
    )
    pre_check = PreCheckConfig(
        name="e2e",
        type="external_api",
        mcp=server,
        mcp_tool="lookup",
        mcp_arguments={"q": "x"},
    )

    async def go():
        import aiohttp

        async with aiohttp.ClientSession() as s:
            return await _fetch_mcp_response(pre_check, {}, HttpRequestExecutor(s))

    payload, reason = asyncio.run(go())
    assert payload is None
    assert "MCP server URL rejected" in (reason or "")
    assert hits == [], "credentialed MCP call must not reach a blocked destination"


def test_mcp_server_url_must_be_https(recorder):
    base, _ = recorder
    with pytest.raises(ssrf.SSRFError, match="(?i)scheme"):
        asyncio.run(ssrf.validate_egress_url(base + "/mcp"))
