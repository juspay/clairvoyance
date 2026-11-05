"""The fetch guard: the caller picks the URL, so this is the SSRF boundary.

Every test here is a way in that must stay shut. They are cheap, and they are
the reason ``POST /assist/probe`` can exist at all.
"""

from __future__ import annotations

import socket
from typing import AsyncIterator, List, Optional, Tuple

import pytest

from app.ai.voice.agents.breeze_buddy.assist.engine.web import fetch
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    FetchFailedError,
    UnsafeUrlError,
    normalize_probe_url,
    resolve_public_addresses,
)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "http://example.com",
        "ftp://example.com",
        "file:///etc/passwd",
        "https://user:pass@example.com",
        "https://example.com:8080",
        "https://localhost",
        "https://api.internal",
        "https://box.local",
        "https://127.0.0.1",
        "https://10.0.0.5",
        "https://192.168.1.1",
        "https://169.254.169.254",  # the cloud metadata service
        "https://[::1]",
        "https://0.0.0.0",
        # Loopback and private ranges wearing an IPv6 costume.
        "https://[::ffff:127.0.0.1]",
        "https://[::ffff:10.0.0.1]",
        "https://[::ffff:169.254.169.254]",
        # A scoped literal names an interface on this machine.
        "https://[fe80::1%25eth0]",
        "https://" + "a" * 3000,
    ],
)
def test_refused_urls(raw: str) -> None:
    with pytest.raises(UnsafeUrlError):
        normalize_probe_url(raw)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("example.com", "https://example.com"),
        ("  https://Example.com/path?q=1  ", "https://Example.com/path?q=1"),
        ("https://shop.example.co.in:443/", "https://shop.example.co.in:443/"),
    ],
)
def test_accepted_urls(raw: str, expected: str) -> None:
    assert normalize_probe_url(raw) == expected


class _FakeLoop:
    """Stands in for the running loop so resolution is decided by the test."""

    def __init__(
        self, addresses: Tuple[str, ...] = (), error: Optional[Exception] = None
    ) -> None:
        self._addresses = addresses
        self._error = error

    async def getaddrinfo(self, host: str, port: int, **_kwargs) -> List[Tuple]:
        if self._error is not None:
            raise self._error
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in self._addresses
        ]


def _resolving(monkeypatch, **kwargs) -> None:
    loop = _FakeLoop(**kwargs)
    monkeypatch.setattr(fetch.asyncio, "get_running_loop", lambda: loop)


async def test_resolution_to_a_private_address_is_refused(monkeypatch) -> None:
    # The name is public and the syntax is fine; the answer is not. This is
    # the shape of a DNS-based bypass, and it stops before any connection.
    _resolving(monkeypatch, addresses=("10.1.2.3",))
    with pytest.raises(UnsafeUrlError):
        await resolve_public_addresses("rebind.example")


async def test_one_private_answer_poisons_the_whole_set(monkeypatch) -> None:
    # Keeping the public answer and dropping the private one would leave the
    # outcome to whichever address the connector happened to try first.
    _resolving(monkeypatch, addresses=("93.184.216.34", "127.0.0.1"))
    with pytest.raises(UnsafeUrlError):
        await resolve_public_addresses("mixed.example")


async def test_the_metadata_service_address_is_refused(monkeypatch) -> None:
    _resolving(monkeypatch, addresses=("169.254.169.254",))
    with pytest.raises(UnsafeUrlError):
        await resolve_public_addresses("metadata.example")


async def test_a_name_that_does_not_resolve_is_a_fetch_failure(monkeypatch) -> None:
    _resolving(monkeypatch, error=socket.gaierror("nope"))
    with pytest.raises(FetchFailedError):
        await resolve_public_addresses("nowhere.example")


async def test_an_empty_answer_is_a_fetch_failure(monkeypatch) -> None:
    _resolving(monkeypatch, addresses=())
    with pytest.raises(FetchFailedError):
        await resolve_public_addresses("empty.example")


async def test_public_answers_are_pinned_for_the_connection(monkeypatch) -> None:
    _resolving(monkeypatch, addresses=("93.184.216.34",))
    answers = await resolve_public_addresses("example.com")
    assert [answer["host"] for answer in answers] == ["93.184.216.34"]

    resolver = fetch._PinnedResolver({("example.com", 443): answers})
    assert await resolver.resolve("example.com", 443, socket.AF_INET) == answers
    # A host the guard never validated cannot be connected to at all, which
    # is what closes the gap between checking a name and using it.
    with pytest.raises(UnsafeUrlError):
        await resolver.resolve("metadata.google.internal", 443, socket.AF_INET)
    await resolver.close()


def test_a_redirect_target_goes_through_the_same_gate() -> None:
    # fetch_page normalizes every hop with this function, so a bounce to a
    # private address is refused exactly like a typed one.
    with pytest.raises(UnsafeUrlError):
        normalize_probe_url("https://169.254.169.254/latest/meta-data/")


class _FakeStream:
    """A response body that arrives in pieces, like a real one does."""

    def __init__(self, chunks: List[bytes]) -> None:
        self._chunks = list(chunks)

    async def iter_chunked(self, n: int) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


async def test_the_whole_body_is_read_not_just_the_first_buffer() -> None:
    # A single read() returns whatever happens to be buffered, which silently
    # truncated real pages to their first few KB — the markers a probe needs
    # sit further down.
    body, truncated = await fetch._read_capped(
        _FakeStream([b"x" * 8192] * 40), 10 * 1024 * 1024
    )
    assert len(body) == 8192 * 40
    assert not truncated


async def test_the_byte_cap_stops_a_body_that_will_not_end() -> None:
    body, truncated = await fetch._read_capped(_FakeStream([b"y" * 8192] * 100), 20_000)
    assert len(body) == 20_000
    assert truncated


async def test_a_mapped_private_address_from_dns_is_refused(monkeypatch) -> None:
    _resolving(monkeypatch, addresses=("::ffff:127.0.0.1",))
    with pytest.raises(UnsafeUrlError):
        await resolve_public_addresses("mapped.example")


def test_a_public_name_containing_internal_words_is_not_blocked() -> None:
    # The suffix list is an early-out for names that can only be internal,
    # not the gate. What settles a public name is what it resolves to, so a
    # legitimate host must not be refused for how it is spelled.
    assert normalize_probe_url("https://api.internal.attacker-shop.com/")
    assert normalize_probe_url("https://local.example.com/")
    assert normalize_probe_url("https://internal-tools.example.com/")


def _pinned_ok(monkeypatch) -> None:
    """Resolution succeeds with a public address, without touching the loop."""

    async def fake(host: str, port: int = 443):
        return [
            fetch.ResolveResult(
                hostname=host,
                host="93.184.216.34",
                port=port,
                family=socket.AF_INET,
                proto=6,
                flags=socket.AI_NUMERICHOST,
            )
        ]

    monkeypatch.setattr(fetch, "resolve_public_addresses", fake)


class _FakeHeaders(dict):
    """Enough of a multidict for the reader: repeated Set-Cookie, plain rest."""

    def __init__(self, values, cookies=()):
        super().__init__(values)
        self._cookies = list(cookies)

    def getall(self, key, default=None):
        if key.lower() == "set-cookie":
            return self._cookies
        value = self.get(key)
        return [value] if value is not None else (default or [])


class _FakeGet:
    def __init__(self, response, recorder):
        self._response = response
        self._recorder = recorder

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *_exc):
        return False

    def __await__(self):
        async def _ready():
            return self._response

        return _ready().__await__()


class _FakeHTTPResponse:
    def __init__(
        self, url, status=200, headers=None, cookies=(), chunks=(b"<html></html>",)
    ):
        self.url = url
        self.status = status
        self.headers = _FakeHeaders(headers or {}, cookies)
        self.charset = "utf-8"
        self.content = _FakeStream(list(chunks))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _RecordingSession:
    """Captures what fetch_page actually put on the wire."""

    calls: List[dict] = []

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def get(self, url, **kwargs):
        _RecordingSession.calls.append({"url": url, **kwargs})
        return _FakeGet(_FakeHTTPResponse(url), _RecordingSession.calls)


async def test_the_request_carries_the_browser_headers(monkeypatch) -> None:
    # Building the headers and not sending them is invisible: the fetch still
    # succeeds, just as an unidentified client, and storefronts serve those a
    # different page than they serve a shopper.
    _RecordingSession.calls = []
    _pinned_ok(monkeypatch)
    monkeypatch.setattr(fetch.aiohttp, "ClientSession", _RecordingSession)

    await fetch.fetch_page("https://shop.example/")

    assert len(_RecordingSession.calls) == 1
    sent = _RecordingSession.calls[0]["headers"]
    assert sent["User-Agent"] == fetch.BROWSER_USER_AGENT
    assert "Accept-Language" in sent
    # Redirects are followed by hand so every hop can be re-validated.
    assert _RecordingSession.calls[0]["allow_redirects"] is False


async def test_a_caller_can_add_headers_without_losing_the_defaults(
    monkeypatch,
) -> None:
    _RecordingSession.calls = []
    _pinned_ok(monkeypatch)
    monkeypatch.setattr(fetch.aiohttp, "ClientSession", _RecordingSession)

    await fetch.fetch_page(
        "https://shop.example/", headers={"Accept": "application/json"}
    )

    sent = _RecordingSession.calls[0]["headers"]
    assert sent["Accept"] == "application/json"
    assert sent["User-Agent"] == fetch.BROWSER_USER_AGENT


async def test_a_proxied_deployment_refuses_to_fetch(monkeypatch) -> None:
    # A proxy is handed the hostname and picks the destination itself, so
    # nothing validated here would bind the connection. Failing loudly beats
    # a guard that looks intact and protects nothing.
    _pinned_ok(monkeypatch)
    monkeypatch.setattr(fetch, "get_proxy_config", lambda: "http://egress:3128")
    with pytest.raises(fetch.EgressNotGuardedError):
        await fetch.fetch_page("https://shop.example/")
