"""Brute-force rate limiting on the credential endpoints (PT-16 hardening).

Covers ``enforce_credential_rate_limit``: per-IP and per-username fixed-window
caps, short-circuit ordering, the empty-identifier case, and the fail-open
posture when Redis is unavailable.
"""

from __future__ import annotations

import hashlib
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


def _decision(allowed: bool) -> RateLimitDecision:
    return RateLimitDecision(allowed=allowed, count=99, limit=40, retry_after_seconds=1)


def _fake_check(deny_buckets: set[str], recorder: list):
    async def _check(*, bucket, identifier, limit, window_seconds, prefix, **kwargs):
        recorder.append((bucket, identifier))
        return _decision(bucket not in deny_buckets)

    return _check


async def test_blocks_when_ip_over_cap(monkeypatch):
    seen: list = []
    monkeypatch.setattr(rl, "check_rate_limit", _fake_check({"credential_ip"}, seen))
    with pytest.raises(HTTPException) as e:
        await rl.enforce_credential_rate_limit(_req(), "alice")
    assert e.value.status_code == 429
    assert (e.value.headers or {}).get("Retry-After") == "1"
    # Must short-circuit on the IP cap — never even touch the username bucket.
    assert [b for b, _ in seen] == ["credential_ip"]


async def test_blocks_when_username_over_cap(monkeypatch):
    seen: list = []
    monkeypatch.setattr(rl, "check_rate_limit", _fake_check({"credential_user"}, seen))
    with pytest.raises(HTTPException) as e:
        await rl.enforce_credential_rate_limit(_req(), "Alice@Example.com")
    assert e.value.status_code == 429
    # Username is normalised (lower/stripped) then SHA-256 hashed before keying.
    expected = hashlib.sha256(b"alice@example.com").hexdigest()
    assert seen[1] == ("credential_user", expected)


async def test_long_username_is_hashed_to_bounded_key(monkeypatch):
    # A pathologically long identifier must not become a giant Redis key: it is
    # hashed to a fixed 64-hex-char digest regardless of input size.
    seen: list = []
    monkeypatch.setattr(rl, "check_rate_limit", _fake_check(set(), seen))
    await rl.enforce_credential_rate_limit(_req(), "x" * 100_000)
    bucket, identifier = seen[1]
    assert bucket == "credential_user"
    assert len(identifier) == 64
    assert identifier == hashlib.sha256(b"x" * 100_000).hexdigest()


async def test_allows_when_under_cap(monkeypatch):
    seen: list = []
    monkeypatch.setattr(rl, "check_rate_limit", _fake_check(set(), seen))
    await rl.enforce_credential_rate_limit(_req(), "bob")  # no raise
    assert [b for b, _ in seen] == ["credential_ip", "credential_user"]


async def test_empty_identifier_skips_username_bucket(monkeypatch):
    seen: list = []
    monkeypatch.setattr(rl, "check_rate_limit", _fake_check(set(), seen))
    await rl.enforce_credential_rate_limit(_req(), None)  # SSO-only, no email
    assert [b for b, _ in seen] == ["credential_ip"]


async def test_ip_key_uses_last_forwarded_hop(monkeypatch):
    # A client-supplied XFF chain must not let the caller escape their bucket:
    # only the last (trusted-proxy-written) hop is used as the identifier.
    seen: list = []
    monkeypatch.setattr(rl, "check_rate_limit", _fake_check(set(), seen))
    await rl.enforce_credential_rate_limit(_req(xff="1.1.1.1, 2.2.2.2, 9.9.9.9"), "bob")
    assert seen[0] == ("credential_ip", hashlib.sha256(b"9.9.9.9").hexdigest())


async def test_ip_is_hashed_before_it_becomes_a_key_or_a_log_field(monkeypatch):
    # An IP address is personal data. check_rate_limit logs its own `identifier`
    # verbatim when Redis is missing or erroring, so the raw value must never be
    # handed to it — the digest goes in instead, and the raw IP appears nowhere.
    seen: list = []
    monkeypatch.setattr(rl, "check_rate_limit", _fake_check(set(), seen))
    await rl.enforce_credential_rate_limit(_req(host="198.51.100.42"), "bob")
    bucket, identifier = seen[0]
    assert bucket == "credential_ip"
    assert identifier == hashlib.sha256(b"198.51.100.42").hexdigest()
    assert "198.51.100.42" not in identifier


async def test_denied_logs_carry_no_raw_identifier(monkeypatch):
    # The 429 log lines are the ones an operator actually reads, and they were
    # the two places raw PII leaked: the client IP and a 64-char prefix of the
    # username. Both must now be digests.
    emitted: list[str] = []
    monkeypatch.setattr(rl.logger, "warning", lambda m, *a, **k: emitted.append(str(m)))

    monkeypatch.setattr(rl, "check_rate_limit", _fake_check({"credential_ip"}, []))
    with pytest.raises(HTTPException):
        await rl.enforce_credential_rate_limit(_req(host="198.51.100.42"), "bob")

    monkeypatch.setattr(rl, "check_rate_limit", _fake_check({"credential_user"}, []))
    with pytest.raises(HTTPException):
        await rl.enforce_credential_rate_limit(
            _req(host="198.51.100.42"), "victim@company.com"
        )

    joined = "\n".join(emitted)
    assert "198.51.100.42" not in joined
    assert "victim@company.com" not in joined
    assert "victim" not in joined
    assert "ip hash" in joined and "identifier hash" in joined


async def test_fails_open_when_redis_unconfigured(monkeypatch):
    # The real check_rate_limit returns allowed=True when Redis isn't configured,
    # so enforcement (which uses the default fail_closed=False) must allow the
    # request — availability of login beats this defence-in-depth layer.
    from app.services.redis import rate_limit as svc

    monkeypatch.setattr(svc, "is_redis_configured", lambda: False)
    await rl.enforce_credential_rate_limit(_req(), "carol")  # no raise
