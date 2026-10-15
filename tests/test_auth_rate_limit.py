"""Brute-force rate limiting on the credential endpoints (PT-16 hardening).

Covers ``credential_attempt``: only FAILED attempts count, per IP and per
username, in a fixed window; a refused request is not itself counted; parallel
guesses cannot all slip under the cap; identifiers are hashed before they reach
Redis or the logs; and the fail-open posture when Redis is unavailable.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException, Request

from app.api.routers.breeze_buddy.auth import rate_limit as rl
from app.services.redis.rate_limit import RateLimitDecision


def _req(xff: str | None = None, host: str = "203.0.113.7") -> Request:
    headers = {"x-forwarded-for": xff} if xff else {}
    return cast(
        Request, SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))
    )


@pytest.fixture
def counters(monkeypatch):
    """An in-memory stand-in for the Redis counters, shared by check and refund."""
    store: Counter = Counter()

    async def _check(*, bucket, identifier, limit, window_seconds, prefix, **kw):
        store[(bucket, identifier)] += 1
        count = store[(bucket, identifier)]
        return RateLimitDecision(
            allowed=count <= limit, count=count, limit=limit, retry_after_seconds=600
        )

    async def _refund(*, bucket, identifier, window_seconds, prefix):
        if store[(bucket, identifier)] > 0:
            store[(bucket, identifier)] -= 1

    monkeypatch.setattr(rl, "check_rate_limit", _check)
    monkeypatch.setattr(rl, "refund_rate_limit", _refund)
    monkeypatch.setattr(rl, "AUTH_RATE_LIMIT_PER_USERNAME", 5)
    monkeypatch.setattr(rl, "AUTH_RATE_LIMIT_PER_IP", 20)
    return store


def _user_key(name: str) -> tuple:
    return ("credential_user", hashlib.sha256(name.encode()).hexdigest())


async def _fail(request, identifier):
    async with rl.credential_attempt(request, identifier):
        raise HTTPException(status_code=401, detail="Invalid username or password")


async def _succeed(request, identifier):
    async with rl.credential_attempt(request, identifier):
        return "token"


async def test_successful_attempts_are_never_counted(counters):
    for _ in range(50):
        assert await _succeed(_req(), "alice") == "token"
    assert counters[_user_key("alice")] == 0


async def test_fifth_failure_locks_the_username(counters):
    for _ in range(5):
        with pytest.raises(HTTPException) as e:
            await _fail(_req(), "alice")
        assert e.value.status_code == 401
    # Even the right password is refused once the failures are used up.
    with pytest.raises(HTTPException) as e:
        await _succeed(_req(), "alice")
    assert e.value.status_code == 429
    assert (e.value.headers or {}).get("Retry-After") == "600"
    assert e.value.detail == (
        "Too many failed sign-in attempts. Please try again in 10 minutes."
    )


async def test_successes_between_failures_do_not_use_up_the_cap(counters):
    for _ in range(4):
        with pytest.raises(HTTPException):
            await _fail(_req(), "alice")
        await _succeed(_req(), "alice")
    assert counters[_user_key("alice")] == 4
    await _succeed(_req(), "alice")  # still allowed


async def test_refused_requests_are_not_counted(counters):
    for _ in range(5):
        with pytest.raises(HTTPException):
            await _fail(_req(), "alice")
    for _ in range(10):
        with pytest.raises(HTTPException) as e:
            await _succeed(_req(), "alice")
        assert e.value.status_code == 429
    # The 429s did not pile onto the counters.
    assert counters[_user_key("alice")] == 5
    ip_key = ("credential_ip", hashlib.sha256(b"203.0.113.7").hexdigest())
    assert counters[ip_key] == 5


async def test_per_ip_failures_are_capped_across_usernames(counters):
    for i in range(20):
        with pytest.raises(HTTPException) as e:
            await _fail(_req(), f"user{i}")
        assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        await _fail(_req(), "user-new")
    assert e.value.status_code == 429
    # Another client is unaffected.
    with pytest.raises(HTTPException) as e:
        await _fail(_req(host="198.51.100.9"), "user-new")
    assert e.value.status_code == 401


async def test_parallel_guesses_cannot_slip_under_the_cap(counters):
    ran = 0

    async def guess():
        nonlocal ran
        async with rl.credential_attempt(_req(), "alice"):
            ran += 1
            await asyncio.sleep(0)
            raise HTTPException(status_code=401)

    results = await asyncio.gather(
        *(guess() for _ in range(30)), return_exceptions=True
    )
    assert ran == 5
    codes = Counter(r.status_code for r in results if isinstance(r, HTTPException))
    assert codes == {401: 5, 429: 25}


async def test_identifiers_are_normalised_and_hashed(counters):
    with pytest.raises(HTTPException):
        await _fail(_req(), "  Alice@Example.com ")
    assert counters[_user_key("alice@example.com")] == 1
    with pytest.raises(HTTPException):
        await _fail(_req(), "x" * 100_000)
    assert counters[_user_key("x" * 100_000)] == 1  # a fixed 64-char digest


async def test_empty_identifier_only_uses_the_ip_cap(counters):
    await _succeed(_req(), None)
    with pytest.raises(HTTPException):
        await _fail(_req(), None)
    assert {b for b, _ in counters} == {"credential_ip"}


async def test_ip_key_is_the_client_the_load_balancer_saw(counters):
    # A client-supplied XFF prefix must not let the caller escape their bucket,
    # and the load balancer's own address must not become everyone's bucket.
    with pytest.raises(HTTPException):
        await _fail(_req(xff="1.1.1.1, 2.2.2.2, 34.111.84.202, 35.191.24.216"), "bob")
    assert counters[("credential_ip", hashlib.sha256(b"2.2.2.2").hexdigest())] == 1


async def test_denied_logs_carry_no_raw_identifier(counters, monkeypatch):
    emitted: list[str] = []
    monkeypatch.setattr(rl.logger, "warning", lambda m, *a, **k: emitted.append(str(m)))
    for _ in range(6):
        with pytest.raises(HTTPException):
            await _fail(_req(host="198.51.100.42"), "victim@company.com")
    joined = "\n".join(emitted)
    assert "credential_user" in joined
    assert "198.51.100.42" not in joined
    assert "victim" not in joined


async def test_fails_open_when_redis_unconfigured(monkeypatch):
    # The real limiter allows everything when Redis isn't configured: login
    # availability beats this defence-in-depth layer.
    from app.services.redis import rate_limit as svc

    monkeypatch.setattr(svc, "is_redis_configured", lambda: False)
    for _ in range(10):
        with pytest.raises(HTTPException) as e:
            await _fail(_req(), "carol")
        assert e.value.status_code == 401


@pytest.mark.parametrize("status_code", [403, 409, 500])
async def test_only_a_401_counts_as_a_failed_attempt(counters, status_code):
    # A taken username on /signup (409), a non-admin with the right password on
    # /auth/s2s/token (403) or a server error are not guesses.
    for _ in range(10):
        with pytest.raises(HTTPException):
            async with rl.credential_attempt(_req(), "alice"):
                raise HTTPException(status_code=status_code)
    assert counters[_user_key("alice")] == 0
    await _succeed(_req(), "alice")  # not locked out


async def test_an_unexpected_error_is_not_counted(counters):
    for _ in range(10):
        with pytest.raises(RuntimeError):
            async with rl.credential_attempt(_req(), "alice"):
                raise RuntimeError("database is down")
    assert counters[_user_key("alice")] == 0
