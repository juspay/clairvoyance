"""What may travel to a redirect target, and how far.

Credentials and the request body belong to the destination the CALLER chose.
Three ways that was not true:

- The drop was computed against the PREVIOUS hop, so a chain A -> B -> B
  dropped on the hop that left A, then compared B against B, saw no change, and
  re-attached the caller's auth and headers to B. An attacker-controlled host
  only had to redirect once more, even to a relative path on itself.
- Only 301/302/303 dropped the body. A 307 replayed the caller's payload to a
  host they never addressed.
- The host comparison used urlparse().hostname, which ignores the port, so
  http://h:8080 -> http://h:9999 counted as the same destination.

The allow-list callers (telephony recordings) and the same-origin caller (the
reporting webhook) were never exposed to the first two: they re-check
absolutely on every hop. HttpRequestExecutor, which carries tenant-configured
Authorization / X-Api-Key headers and no allow-list, was.
"""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web

import app.core.security.ssrf as ssrf_mod
from app.core.security.ssrf import SSRFError, is_same_origin, ssrf_safe_request

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
    # A -> B/second -> B/final. Hop 1 leaves A; hop 2 stays on B. The drop must
    # latch, not be recomputed from the hop before it.
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
    # 307 preserves method and body by the spec. That must not mean replaying
    # the caller's payload somewhere they never addressed.
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
    # The counterpart: an endpoint normalising its own path must still be
    # delivered to, or the guard breaks working integrations.
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
    # A scheme upgrade counts only between the default ports; :8080 -> :9443 is
    # a different service on one machine.
    assert is_same_origin("http://shop.com/a", "https://shop.com/a")
    assert not is_same_origin("http://shop.com:8080/a", "https://shop.com:9443/a")
    assert not is_same_origin("https://shop.com/a", "http://shop.com/a")  # downgrade


@pytest.mark.parametrize(
    "url",
    ["https://example.com:99999/x", "https://example.com:abc/x"],
)
async def test_a_malformed_port_is_refused_as_an_ssrf_error(url):
    # urlparse defers port parsing to attribute access. A bare ValueError here
    # slips past every `except SSRFError` handler — the HTTP executor would log
    # it as an unexpected error and retry it.
    with pytest.raises(SSRFError, match="[Pp]ort"):
        await ssrf_mod.validate_egress_url(url)
