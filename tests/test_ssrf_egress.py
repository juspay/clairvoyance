"""SSRF egress guard (PT-03/05/07/11).

Covers the shared ``app.core.security.ssrf`` validator: scheme enforcement,
IP-literal blocking, DNS-name resolution + validation (the bypass the pentest
flagged), and the redirect-following helper's per-hop revalidation.
"""

from __future__ import annotations

import socket
from typing import cast

import aiohttp
import pytest

from app.core.security import ssrf
from app.core.security.ssrf import (
    SSRFError,
    host_matches_allowlist,
    ip_block_reason,
    ssrf_safe_request,
    validate_egress_url,
)


def test_ip_block_reason_flags_metadata_and_private():
    assert ip_block_reason("169.254.169.254")  # cloud metadata (link-local)
    assert ip_block_reason("10.1.2.3")
    assert ip_block_reason("127.0.0.1")
    assert ip_block_reason("::1")
    assert ip_block_reason("fd00::1")  # IPv6 ULA
    assert ip_block_reason("::ffff:169.254.169.254")  # IPv4-mapped metadata
    assert ip_block_reason("1.1.1.1") is None  # public


async def test_validate_egress_url_rejects_http_scheme():
    with pytest.raises(SSRFError):
        await validate_egress_url("http://example.com/x")


async def test_validate_egress_url_rejects_ip_literal_private():
    for url in (
        "https://10.0.0.5/x",
        "https://127.0.0.1/x",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/x",
    ):
        with pytest.raises(SSRFError):
            await validate_egress_url(url)


async def test_validate_egress_url_allows_public_ip_literal():
    assert await validate_egress_url("https://1.1.1.1/x") == ["1.1.1.1"]


async def test_validate_egress_url_ip_literal_private_short_circuits(monkeypatch):
    # Regression: SSRFError subclasses ValueError, so a blocked IP literal must
    # be raised by the fast path directly — never swallowed and re-resolved via
    # getaddrinfo (which would be a fragile, dead fast path).
    def _fail(*a, **k):
        raise AssertionError("resolution must not run for a blocked IP literal")

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", _fail)
    with pytest.raises(SSRFError):
        await validate_egress_url("https://127.0.0.1/x")


async def test_validate_egress_url_blocks_dns_name_resolving_to_internal(monkeypatch):
    # The exact bypass PT-07 describes: a DNS name that resolves to an internal
    # address must be rejected, not allowed on the "it's not an IP literal" path.
    def fake_getaddrinfo(host, port, *a, **k):
        return [(socket.AF_INET, None, None, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SSRFError):
        await validate_egress_url("https://evil.internal.test/x")


async def test_validate_egress_url_allows_dns_name_resolving_to_public(monkeypatch):
    def fake_getaddrinfo(host, port, *a, **k):
        return [(socket.AF_INET, None, None, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)
    assert await validate_egress_url("https://api.example.com/x") == ["8.8.8.8"]


async def test_validate_egress_url_fails_closed_on_resolution_error(monkeypatch):
    def boom(host, port, *a, **k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", boom)
    with pytest.raises(SSRFError):
        await validate_egress_url("https://does-not-resolve.example/x")


def test_host_allowlist_suffix_confusion_is_rejected():
    assert host_matches_allowlist("https://api.twilio.com/x", ["twilio.com"])
    assert host_matches_allowlist("https://twilio.com/x", ["twilio.com"])
    # Must use a dot boundary — a lookalike domain must NOT match.
    assert not host_matches_allowlist(
        "https://evil-twilio.com.attacker.net/x", ["twilio.com"]
    )
    assert not host_matches_allowlist("https://nottwilio.com/x", ["twilio.com"])


# ── ssrf_safe_request: per-hop redirect revalidation (the core PT-07 fix) ────
class _FakeResp:
    """Minimal stand-in for aiohttp.ClientResponse used by ssrf_safe_request."""

    def __init__(self, status: int, headers: dict | None = None):
        self.status = status
        self.headers = headers or {}
        self.released = False

    def release(self) -> None:
        self.released = True


class _FakeSession:
    """Returns queued responses and records the auth each hop was issued with.

    Public IP literals (8.8.8.8 / 1.1.1.1) are used as hosts so ssrf_safe_request
    validates them without any DNS resolution.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[dict] = []

    async def request(self, method, url, *, auth=None, allow_redirects=True, **kwargs):
        self.requests.append({"method": method, "url": url, "auth": auth})
        return self._responses.pop(0)


async def test_ssrf_safe_request_blocks_redirect_to_metadata():
    # A 302 whose Location is the cloud-metadata address must be blocked at the
    # redirect hop. aiohttp's own redirect following would skip the check — this
    # helper re-validates every hop, so the metadata fetch never goes out.
    session = _FakeSession([_FakeResp(302, {"Location": "https://169.254.169.254/"})])
    with pytest.raises(SSRFError):
        async with ssrf_safe_request(
            cast(aiohttp.ClientSession, session), "GET", "https://8.8.8.8/start"
        ):
            pass  # pragma: no cover - the redirect hop must raise before the body
    # Only the first (public) hop was issued; the metadata hop was rejected
    # before any request left the process.
    assert len(session.requests) == 1


async def test_ssrf_safe_request_strips_auth_on_offsite_redirect():
    # A redirect leaving the allow-list must drop credentials, so a 302 can't
    # exfiltrate provider creds to an attacker-controlled (but public) host.
    auth = aiohttp.BasicAuth("user", "secret")
    session = _FakeSession(
        [
            _FakeResp(302, {"Location": "https://1.1.1.1/next"}),
            _FakeResp(200, {}),
        ]
    )
    async with ssrf_safe_request(
        cast(aiohttp.ClientSession, session),
        "GET",
        "https://8.8.8.8/start",
        auth=auth,
        allowed_host_suffixes=["8.8.8.8"],
    ) as resp:
        assert resp.status == 200
    # First hop (on the allow-list) carried the creds; the off-allow-list
    # redirect target was fetched with auth stripped.
    assert session.requests[0]["auth"] is auth
    assert session.requests[1]["auth"] is None
