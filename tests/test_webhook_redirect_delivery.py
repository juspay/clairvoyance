"""Reporting webhooks survive a redirect on the tenant's own host.

Before this, ``max_redirects=0`` aborted any 3xx — a merchant whose endpoint
answers /hook with a 301 to /hook/ stopped receiving call outcomes entirely.
The actual risk is the signed payload reaching a host the tenant never
configured, not the redirect itself, so same-host hops are followed and
off-origin ones are not; every hop is still validated.
"""

from __future__ import annotations

import json

import aiohttp
import pytest
from aiohttp import web

import app.core.network.egress as ssrf_mod
from app.ai.voice.agents.breeze_buddy.utils.common import send_webhook_with_retry
from app.core.network import SSRFError, is_same_origin, ssrf_safe_request

PAYLOAD = {"orderId": "SHOP-1234", "outcome": "CONFIRMED", "transcription": "yes"}


@pytest.fixture(autouse=True)
def _allow_loopback(monkeypatch):
    monkeypatch.setattr("app.core.network.egress._ALLOW_PRIVATE_EGRESS", True)


async def _endpoint(redirect_status: int | None, *, location: str = "/hook/"):
    """A merchant endpoint that optionally redirects before accepting."""
    got: dict = {}

    async def hook(request: web.Request) -> web.Response:
        if redirect_status is None:
            return await final(request)
        return web.Response(status=redirect_status, headers={"Location": location})

    async def final(request: web.Request) -> web.Response:
        got["method"] = request.method
        got["body"] = await request.text()
        got["checksum"] = request.headers.get("checksum")
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_route("*", "/hook", hook)
    app.router.add_route("*", "/hook/", final)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}", got


async def test_a_307_on_the_same_host_still_delivers_the_payload():
    # Regression: worked pre-guard, broke after. 307 preserves method and body.
    runner, base, got = await _endpoint(307)
    try:
        ok = await send_webhook_with_retry(f"{base}/hook", PAYLOAD)
    finally:
        await runner.cleanup()

    assert ok is True
    assert got["method"] == "POST"
    assert json.loads(got["body"]) == PAYLOAD


async def test_a_301_on_the_same_host_is_not_called_delivered():
    """301/302/303 turn the POST into a bodyless GET by the HTTP spec.

    Following it reaches the endpoint with no outcome in it, and a 200 to that
    empty GET would be reported as success — the merchant sees a delivered
    webhook and has nothing. Refuse instead, so the URL gets fixed.
    """
    runner, base, got = await _endpoint(301)
    try:
        delivered = await send_webhook_with_retry(f"{base}/hook", PAYLOAD)
    finally:
        await runner.cleanup()

    assert delivered is False
    assert got == {}, "the bodyless GET must not even be sent"


async def test_no_redirect_still_delivers():
    runner, base, got = await _endpoint(None)
    try:
        ok = await send_webhook_with_retry(f"{base}/hook", PAYLOAD)
    finally:
        await runner.cleanup()

    assert ok is True and got["method"] == "POST"
    assert json.loads(got["body"]) == PAYLOAD


async def test_an_off_host_redirect_never_receives_the_payload():
    # Payload must not reach the off-host server; delivery must report failure.
    other, other_base, other_got = await _endpoint(None)
    runner, base, _ = await _endpoint(302, location=f"{other_base}/hook")
    try:
        ok = await send_webhook_with_retry(f"{base}/hook", PAYLOAD)
    finally:
        await runner.cleanup()
        await other.cleanup()

    assert ok is False
    assert other_got == {}, "payload reached a host the tenant never configured"


async def test_the_hop_budget_still_applies_on_the_same_host():
    async def loop_hop(request: web.Request) -> web.Response:
        return web.Response(status=307, headers={"Location": "/hook"})

    app = web.Application()
    app.router.add_route("*", "/hook", loop_hop)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(SSRFError, match="Too many redirects"):
                async with ssrf_safe_request(
                    session,
                    "POST",
                    f"{base}/hook",
                    json=PAYLOAD,
                    allow_http=True,
                    max_redirects=2,
                    same_origin_only=True,
                ) as resp:
                    await resp.text()
    finally:
        await runner.cleanup()


def test_same_origin_allows_an_https_upgrade_but_not_a_downgrade():
    assert is_same_origin("http://shop.com/hook", "https://shop.com/hook")
    assert not is_same_origin("https://shop.com/hook", "http://shop.com/hook")


def test_same_origin_rejects_a_different_host_or_port():
    assert is_same_origin("https://shop.com/hook", "https://shop.com/hook/")
    assert not is_same_origin("https://shop.com/hook", "https://evil.com/hook")
    assert not is_same_origin("https://shop.com/hook", "https://shop.com:8443/hook")
    # A subdomain is a different host, not a "close enough" one.
    assert not is_same_origin("https://shop.com/hook", "https://api.shop.com/hook")


async def test_a_resolver_blip_is_retried_not_treated_as_a_refusal(monkeypatch):
    """EgressResolutionError subclasses SSRFError, so catching only SSRFError
    ends the send on its first attempt. Before the guard existed a DNS failure
    surfaced from session.post as a ClientConnectorError and was retried."""
    attempts: list = []

    async def flaky(hostname: str, port: int):
        attempts.append(hostname)
        raise OSError("temporary failure in name resolution")

    monkeypatch.setattr(ssrf_mod, "_resolve_host", flaky)
    monkeypatch.setattr(ssrf_mod, "_ALLOW_PRIVATE_EGRESS", False)

    delivered = await send_webhook_with_retry("https://merchant.example/hook", PAYLOAD)

    assert delivered is False
    assert len(attempts) == 3, f"expected all 3 attempts, got {len(attempts)}"
