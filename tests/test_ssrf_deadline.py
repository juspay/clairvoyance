"""ssrf_safe_request — one deadline for the whole redirect chain.

``ClientTimeout(total=N)`` means N seconds for the operation. aiohttp honours
that across redirects because it follows them inside a single request() call.
``ssrf_safe_request`` follows them itself, so unless the budget is tracked
across hops each hop starts a fresh N-second clock and the caller's number
stops being a ceiling: the remote server picks the hop count, so the caller
cannot even predict the worst case.

Every test here uses a server whose hops are individually FAST ENOUGH (each
under the budget) but collectively too slow. A per-hop clock never fires on
such a chain; a shared deadline does. That is the whole difference.
"""

from __future__ import annotations

import asyncio
import time

import aiohttp
import pytest
from aiohttp import web

from app.core.security.ssrf import ssrf_safe_request


@pytest.fixture(autouse=True)
def _allow_loopback(monkeypatch):
    """These servers live on 127.0.0.1; the egress guard blocks that by design."""
    monkeypatch.setattr("app.core.security.ssrf._ALLOW_PRIVATE_EGRESS", True)


async def _chain(hops: int, per_hop_delay: float):
    """A server that redirects `hops` times, sleeping before each response."""

    async def hop(request: web.Request) -> web.Response:
        n = int(request.match_info["n"])
        await asyncio.sleep(per_hop_delay)
        if n < hops:
            raise web.HTTPFound(location=f"/hop/{n + 1}")
        return web.Response(text="final")

    app = web.Application()
    app.router.add_route("*", "/hop/{n}", hop)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}/hop/0"


async def test_the_caller_s_total_covers_the_whole_chain_not_each_hop():
    # 4 hops x 0.4s = 1.6s of server time, budget 0.9s. No single hop exceeds
    # 0.9s, so only a shared deadline can stop this.
    runner, url = await _chain(hops=3, per_hop_delay=0.4)
    try:
        async with aiohttp.ClientSession() as session:
            started = time.perf_counter()
            with pytest.raises(asyncio.TimeoutError):
                async with ssrf_safe_request(
                    session,
                    "GET",
                    url,
                    allow_http=True,
                    max_redirects=3,
                    timeout=aiohttp.ClientTimeout(total=0.9),
                ) as resp:
                    await resp.text()
            elapsed = time.perf_counter() - started
        # Bounded by the budget, not by 4 x 0.9s.
        assert elapsed < 1.5, f"took {elapsed:.2f}s, budget was 0.9s"
    finally:
        await runner.cleanup()


async def test_a_chain_inside_the_budget_still_succeeds():
    # 4 hops x 0.1s = 0.4s, budget 3s. Nothing should be cut short.
    runner, url = await _chain(hops=3, per_hop_delay=0.1)
    try:
        async with aiohttp.ClientSession() as session:
            async with ssrf_safe_request(
                session,
                "GET",
                url,
                allow_http=True,
                max_redirects=3,
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                assert resp.status == 200
                assert await resp.text() == "final"
    finally:
        await runner.cleanup()


async def test_the_session_default_is_used_when_the_caller_passes_no_timeout():
    # The recording downloads pass no timeout at all, so the session's own
    # ClientTimeout is the only budget they have. It must cover the chain too.
    runner, url = await _chain(hops=3, per_hop_delay=0.4)
    try:
        timeout = aiohttp.ClientTimeout(total=0.9)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            started = time.perf_counter()
            with pytest.raises(asyncio.TimeoutError):
                async with ssrf_safe_request(
                    session, "GET", url, allow_http=True, max_redirects=3
                ) as resp:
                    await resp.text()
            elapsed = time.perf_counter() - started
        assert elapsed < 1.5, f"took {elapsed:.2f}s, session budget was 0.9s"
    finally:
        await runner.cleanup()


async def test_no_budget_anywhere_leaves_behaviour_unchanged():
    # total=None means "no overall limit". Nothing to divide, nothing to clamp.
    runner, url = await _chain(hops=2, per_hop_delay=0.05)
    try:
        timeout = aiohttp.ClientTimeout(total=None)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with ssrf_safe_request(
                session, "GET", url, allow_http=True, max_redirects=3
            ) as resp:
                assert resp.status == 200
    finally:
        await runner.cleanup()


async def test_the_other_timeout_fields_survive_the_clamp():
    # Only `total` shrinks per hop; connect/sock_read/sock_connect are the
    # caller's and must reach the request untouched.
    seen: list = []  # ClientTimeout per hop, as aiohttp received it
    runner, url = await _chain(hops=1, per_hop_delay=0.05)
    try:
        async with aiohttp.ClientSession() as session:
            original = session.request

            def spy(method, u, **kw):
                seen.append(kw.get("timeout"))
                return original(method, u, **kw)

            session.request = spy  # type: ignore[method-assign]
            async with ssrf_safe_request(
                session,
                "GET",
                url,
                allow_http=True,
                max_redirects=2,
                timeout=aiohttp.ClientTimeout(
                    total=5, connect=3, sock_read=4, sock_connect=2
                ),
            ) as resp:
                assert resp.status == 200
    finally:
        await runner.cleanup()

    assert len(seen) == 2, "expected one request per hop"
    for t in seen:
        assert t is not None
        assert t.connect == 3 and t.sock_read == 4 and t.sock_connect == 2
    # And the budget actually shrank between hops.
    assert seen[1].total is not None and seen[0].total is not None
    assert seen[1].total < seen[0].total <= 5
