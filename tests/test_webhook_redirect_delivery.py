"""Reporting webhooks survive a redirect on the tenant's own host.

The egress guard's first cut passed ``max_redirects=0``, so any 3xx aborted
delivery: a merchant whose endpoint answers /hook with a 301 to /hook/, or
upgrades http to https, stopped receiving call outcomes entirely — transcript
included — and the log blamed the egress guard for it.

The risk being guarded is narrower than "a redirect": it is the signed payload
being re-sent to a host the tenant never configured. A redirect that stays on
their own host cannot do that, so those are followed and off-origin ones are
not. Every hop is still validated by the egress guard either way.
"""

from __future__ import annotations

import json

import aiohttp
import pytest
from aiohttp import web

from app.ai.voice.agents.breeze_buddy.utils.common import send_webhook_with_retry
from app.core.security.ssrf import SSRFError, is_same_origin, ssrf_safe_request

PAYLOAD = {"orderId": "SHOP-1234", "outcome": "CONFIRMED", "transcription": "yes"}


@pytest.fixture(autouse=True)
def _allow_loopback(monkeypatch):
    monkeypatch.setattr("app.core.security.ssrf._ALLOW_PRIVATE_EGRESS", True)


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
    # The regression: this worked before the egress guard and stopped working
    # after it. 307 is the redirect that preserves method and body.
    runner, base, got = await _endpoint(307)
    try:
        async with aiohttp.ClientSession() as session:
            ok = await send_webhook_with_retry(session, f"{base}/hook", PAYLOAD)
    finally:
        await runner.cleanup()

    assert ok is True
    assert got["method"] == "POST"
    assert json.loads(got["body"]) == PAYLOAD


async def test_a_301_on_the_same_host_is_followed():
    # Followed, and — per the HTTP spec, unchanged from before the guard —
    # arrives as a bodyless GET. Restoring the pre-guard behaviour exactly;
    # the empty-body quirk of 301 is not this change's to fix.
    runner, base, got = await _endpoint(301)
    try:
        async with aiohttp.ClientSession() as session:
            await send_webhook_with_retry(session, f"{base}/hook", PAYLOAD)
    finally:
        await runner.cleanup()

    assert got["method"] == "GET"


async def test_no_redirect_still_delivers():
    runner, base, got = await _endpoint(None)
    try:
        async with aiohttp.ClientSession() as session:
            ok = await send_webhook_with_retry(session, f"{base}/hook", PAYLOAD)
    finally:
        await runner.cleanup()

    assert ok is True and got["method"] == "POST"
    assert json.loads(got["body"]) == PAYLOAD


async def test_an_off_host_redirect_never_receives_the_payload():
    # Two servers: the tenant's, and somewhere else. The payload must not reach
    # the second one, and delivery must report failure rather than success.
    other, other_base, other_got = await _endpoint(None)
    runner, base, _ = await _endpoint(302, location=f"{other_base}/hook")
    try:
        async with aiohttp.ClientSession() as session:
            ok = await send_webhook_with_retry(session, f"{base}/hook", PAYLOAD)
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
