"""What may travel to a redirect target, and how far.

Credentials and the request body belong to the destination the CALLER chose.
Three ways that was not true, now regression-tested here:

- The drop was computed against the PREVIOUS hop, so a chain A -> B -> B
  re-attached auth/headers on the second hop (B vs B looked unchanged).
- Only 301/302/303 dropped the body; a 307 replayed the payload off-host.
- The host comparison ignored the port, so :8080 -> :9999 looked identical.

HttpRequestExecutor (tenant Authorization/X-Api-Key headers, no allow-list)
was exposed to the first two; allow-list and same-origin callers re-check
absolutely on every hop and were not.
"""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web

import app.core.network.egress as ssrf_mod
from app.core.network import SSRFError, is_same_origin, ssrf_safe_request

SECRET_HEADER = "TENANT-SECRET"


@pytest.fixture(autouse=True)
def _allow_loopback(monkeypatch):
    """These servers live on 127.0.0.1/localhost; the guard blocks that by design."""
    monkeypatch.setattr(ssrf_mod, "_ALLOW_PRIVATE_EGRESS", True)


async def _host(seen: list, *, label: str, onward: dict[str, str], status: int = 302):
    """A server recording what each request carried, then redirecting onward.

    ``onward`` maps path -> Location. A path absent from it is terminal.
    """

    async def handle(request: web.Request) -> web.Response:
        seen.append(
            {
                "label": label,
                "path": request.path,
                "method": request.method,
                "auth": request.headers.get("Authorization"),
                "api_key": request.headers.get("X-Api-Key"),
                "host": request.headers.get("Host", ""),
                "query": request.rel_url.query_string,
                "cookie": request.headers.get("Cookie"),
                "body": await request.text(),
            }
        )
        location = onward.get(request.path)
        if location:
            return web.Response(status=status, headers={"Location": location})
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, runner.addresses[0][1]


async def test_credentials_do_not_come_back_after_leaving_the_callers_host():
    # A -> B/second -> B/final; the drop must latch, not recompute per hop.
    seen: list = []
    b_runner, b_port = await _host(seen, label="B", onward={"/second": "/final"})
    b_base = f"http://localhost:{b_port}"
    a_runner, a_port = await _host(
        seen, label="A", onward={"/start": b_base + "/second"}
    )
    start = f"http://127.0.0.1:{a_port}/start"

    try:
        async with aiohttp.ClientSession() as session:
            async with ssrf_safe_request(
                session,
                "GET",
                start,
                auth=aiohttp.BasicAuth("user", "pass"),
                headers={"X-Api-Key": SECRET_HEADER},
                allow_http=True,
                max_redirects=3,
            ) as resp:
                await resp.text()
    finally:
        await a_runner.cleanup()
        await b_runner.cleanup()

    assert [s["label"] for s in seen] == ["A", "B", "B"]
    assert seen[0]["auth"] and seen[0]["api_key"] == SECRET_HEADER  # the chosen host
    for hop in seen[1:]:
        assert hop["auth"] is None, f"auth reached {hop['label']}{hop['path']}"
        assert hop["api_key"] is None, f"api key reached {hop['label']}{hop['path']}"


async def test_credentials_survive_a_redirect_that_stays_on_the_callers_host():
    # Same host, different path — nothing has left the caller's destination.
    seen: list = []
    runner, port = await _host(seen, label="A", onward={"/start": "/final"})
    start = f"http://127.0.0.1:{port}/start"

    try:
        async with aiohttp.ClientSession() as session:
            async with ssrf_safe_request(
                session,
                "GET",
                start,
                auth=aiohttp.BasicAuth("user", "pass"),
                headers={"X-Api-Key": SECRET_HEADER},
                allow_http=True,
                max_redirects=3,
            ) as resp:
                await resp.text()
    finally:
        await runner.cleanup()

    assert len(seen) == 2
    assert all(h["api_key"] == SECRET_HEADER and h["auth"] for h in seen)


async def test_a_307_does_not_replay_the_body_to_another_host():
    # 307 preserves method+body by spec; that must not mean replaying it off-host.
    seen: list = []
    b_runner, b_port = await _host(seen, label="B", onward={})
    b_base = f"http://localhost:{b_port}"
    a_runner, a_port = await _host(
        seen, label="A", onward={"/start": b_base + "/final"}, status=307
    )
    start = f"http://127.0.0.1:{a_port}/start"

    try:
        async with aiohttp.ClientSession() as session:
            async with ssrf_safe_request(
                session,
                "POST",
                start,
                json={"pii": "customer-address"},
                allow_http=True,
                max_redirects=2,
            ) as resp:
                await resp.text()
    finally:
        await a_runner.cleanup()
        await b_runner.cleanup()

    assert "customer-address" in seen[0]["body"]  # the host the caller chose
    assert seen[1]["label"] == "B"
    assert "customer-address" not in seen[1]["body"], "payload replayed off-host"


async def test_a_307_keeps_the_body_on_the_callers_own_host():
    # Counterpart: an endpoint normalising its own path must still get delivered to.
    seen: list = []
    runner, port = await _host(seen, label="A", onward={"/hook": "/hook/"}, status=307)
    start = f"http://127.0.0.1:{port}/hook"

    try:
        async with aiohttp.ClientSession() as session:
            async with ssrf_safe_request(
                session,
                "POST",
                start,
                json={"outcome": "CONFIRMED"},
                allow_http=True,
                max_redirects=2,
            ) as resp:
                await resp.text()
    finally:
        await runner.cleanup()

    assert seen[-1]["method"] == "POST"
    assert "CONFIRMED" in seen[-1]["body"]


def test_same_origin_compares_the_port_not_just_the_host():
    assert is_same_origin("https://shop.com/a", "https://shop.com/b")
    assert not is_same_origin("http://shop.com:8080/a", "http://shop.com:9999/a")
    # A scheme upgrade counts only between the default ports.
    assert is_same_origin("http://shop.com/a", "https://shop.com/a")
    assert not is_same_origin("http://shop.com:8080/a", "https://shop.com:9443/a")
    assert not is_same_origin("https://shop.com/a", "http://shop.com/a")  # downgrade


@pytest.mark.parametrize(
    "url",
    ["https://example.com:99999/x", "https://example.com:abc/x"],
)
async def test_a_malformed_port_is_refused_as_an_ssrf_error(url):
    # A bad port raises bare ValueError here, slipping past `except SSRFError`.
    with pytest.raises(SSRFError, match="[Pp]ort"):
        await ssrf_mod.validate_egress_url(url)


async def test_query_params_and_cookies_do_not_follow_a_hop_off_host():
    """Credentials are not only headers.

    aiohttp merges ``params`` into the redirect target's query string and sends
    ``cookies`` whatever the host, so a query token or session cookie reaches a
    host the caller never chose unless both are dropped with ``auth``.
    """
    seen: list = []
    b_runner, b_port = await _host(seen, label="B", onward={})
    b_base = f"http://localhost:{b_port}"
    a_runner, a_port = await _host(
        seen, label="A", onward={"/start": b_base + "/final"}
    )
    start = f"http://127.0.0.1:{a_port}/start"

    try:
        async with aiohttp.ClientSession() as session:
            async with ssrf_safe_request(
                session,
                "GET",
                start,
                params={"token": "QUERY_SECRET"},
                cookies={"sess": "COOKIE_SECRET"},
                allow_http=True,
                max_redirects=2,
            ) as resp:
                await resp.text()
    finally:
        await a_runner.cleanup()
        await b_runner.cleanup()

    assert "QUERY_SECRET" in seen[0]["query"]  # the host the caller chose
    assert "QUERY_SECRET" not in seen[1]["query"], "query token reached another host"
    assert seen[1]["cookie"] is None, "cookie reached another host"


@pytest.mark.parametrize(
    "url",
    ["https://" + "a" * 64 + ".com/x", "https://exa mple.com/x"],
)
async def test_a_hostname_that_cannot_be_encoded_is_refused_not_raised(url):
    """getaddrinfo raises UnicodeError for a bad IDNA label.

    UnicodeError is a ValueError but not an OSError, so it escaped the resolve
    handler and every caller's `except SSRFError` with it.
    """
    with pytest.raises(SSRFError):
        await ssrf_mod.validate_egress_url(url)


async def test_the_connection_uses_the_address_that_was_validated():
    """A rebinding host answers public to the validator and private to the
    connector. The request must go to the address that was checked.

    The name here does not exist in DNS at all, so the request can only succeed
    by connecting to what validate_egress_url returned. This also covers the
    proxy case: a proxy is handed a literal address rather than a name to
    resolve on our behalf.
    """
    seen: list = []
    runner, port = await _host(seen, label="A", onward={})

    async def only_the_validator_knows(hostname: str, port: int):
        return ["127.0.0.1"]

    import app.core.network.egress as mod

    original = mod._resolve_host
    mod._resolve_host = only_the_validator_knows
    try:
        async with aiohttp.ClientSession() as session:
            async with ssrf_safe_request(
                session,
                "GET",
                f"http://nowhere-in-dns.invalid:{port}/x",
                allow_http=True,
            ) as resp:
                assert resp.status == 200
    finally:
        mod._resolve_host = original
        await runner.cleanup()

    # The real host still reaches the server for routing and TLS.
    assert seen[0]["host"].startswith("nowhere-in-dns.invalid")


@pytest.mark.parametrize(
    "host",
    ["straße.attacker.example", "xς.attacker.example"],
)
async def test_the_name_validated_is_the_name_aiohttp_would_resolve(host, monkeypatch):
    """urlparse and yarl disagree on internationalised hosts.

    The stdlib idna codec follows IDNA 2003, yarl follows IDNA 2008/UTS46, so
    they can produce different DNS names for the same URL — an attacker points
    one at a public address and the other inward. The validator must check the
    name aiohttp will actually resolve.
    """
    asked: list = []

    async def record(hostname: str, port: int):
        asked.append(hostname)
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_mod, "_resolve_host", record)
    await ssrf_mod.validate_egress_url(f"https://{host}/x")

    from yarl import URL

    assert asked == [URL(f"https://{host}/x").raw_host]
