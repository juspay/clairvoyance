"""The daily health-restore must be safe in the one way that matters.

Clairvoyance's DragonTTS flag is one-way: its monitor and our drain both only
ever mark us unhealthy, so nothing re-enables caching. :mod:`app.health_restore`
closes that loop once a day — which means it can also re-enable caching against
a DragonTTS that is genuinely broken.

The load-bearing rule is therefore: restore ONLY a flag positively read as
"unhealthy". Every other reading — transport error, non-200, non-JSON, a value
we do not recognise — is UNKNOWN and must leave the flag alone. These tests pin
that, plus the once-per-day claim (so N uvicorn workers act once, and a failed
attempt can retry the same day).
"""

from __future__ import annotations

import httpx
import pytest

from app import health_restore
from app.core.config import settings
from app.storage.sqlite import HEALTH_RESTORE_CLAIM

STATUS = "https://clairvoyance.test/agent/voice/breeze-buddy/admin/dragontts/status"
MANAGE = "https://clairvoyance.test/agent/voice/breeze-buddy/admin/dragontts/manage"


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    """Point the job at a fake clairvoyance and enable it."""
    monkeypatch.setattr(settings, "clairvoyance_url", "https://clairvoyance.test")
    monkeypatch.setattr(settings, "clairvoyance_jwt_token", "test-jwt")
    monkeypatch.setattr(settings, "health_restore_enabled", True)


class FakeMetadata:
    """Records claim/release so the once-per-day contract is observable."""

    def __init__(self, win: bool = True) -> None:
        self.win = win
        self.claims: list[tuple[str, str]] = []
        self.releases: list[str] = []

    async def claim_daily(self, key: str, today: str) -> bool:
        self.claims.append((key, today))
        return self.win

    async def release_daily(self, key: str) -> None:
        self.releases.append(key)


class FakeCache:
    def __init__(self, metadata: FakeMetadata) -> None:
        self._metadata = metadata


def _cache(win: bool = True) -> FakeCache:
    return FakeCache(FakeMetadata(win))


def _transport(handler):
    """Install a fake httpx transport for the duration of one run."""
    return httpx.MockTransport(handler)


@pytest.fixture
def route(monkeypatch):
    """Route the job's httpx client to a handler; returns the request log."""
    calls: list[httpx.Request] = []

    def install(handler):
        def wrapped(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return handler(request)

        real_client = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = _transport(wrapped)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(health_restore.httpx, "AsyncClient", factory)
        return calls

    return install


def _posted(calls) -> bool:
    return any(c.method == "POST" for c in calls)


# ---------------------------------------------------------------------------
# the rule: restore ONLY a positively-read "unhealthy"
# ---------------------------------------------------------------------------


async def test_restores_when_flag_reads_unhealthy(route):
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"action": "restore", "health": "healthy"})
        seen["n"] += 1
        # unhealthy first, healthy on the confirmation read
        health = "unhealthy" if seen["n"] == 1 else "healthy"
        return httpx.Response(200, json={"health": health})

    calls = route(handler)
    cache = _cache()
    assert await health_restore.run_daily_restore(cache, force=True) is True
    assert _posted(calls)


async def test_already_healthy_does_nothing(route):
    calls = route(lambda r: httpx.Response(200, json={"health": "healthy"}))
    cache = _cache()
    assert await health_restore.run_daily_restore(cache, force=True) is False
    assert not _posted(calls), "must not POST when the flag is already healthy"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(401, json={"detail": "unauthorized"}),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"health": "weird"}),
        httpx.Response(200, json={}),
    ],
    ids=["http-500", "http-401", "non-json", "unknown-value", "missing-field"],
)
async def test_unknown_state_never_restores(route, response):
    """UNKNOWN must fail closed — re-enabling caching blind is the worse error."""
    calls = route(lambda r: response)
    cache = _cache()
    assert await health_restore.run_daily_restore(cache, force=True) is False
    assert not _posted(calls), "unknown state must never trigger a restore"


async def test_transport_failure_never_restores(route):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    calls = route(handler)
    cache = _cache()
    assert await health_restore.run_daily_restore(cache, force=True) is False
    assert not _posted(calls)


async def test_restore_rejected_reports_failure(route):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(503, text="service unavailable")
        return httpx.Response(200, json={"health": "unhealthy"})

    route(handler)
    assert await health_restore.run_daily_restore(_cache(), force=True) is False


# ---------------------------------------------------------------------------
# once-per-day claim across uvicorn workers
# ---------------------------------------------------------------------------


async def test_loses_claim_means_another_worker_already_ran(route, monkeypatch):
    monkeypatch.setattr(health_restore, "_past_target_time", lambda: True)
    calls = route(lambda r: httpx.Response(200, json={"health": "unhealthy"}))
    cache = _cache(win=False)
    assert await health_restore.run_daily_restore(cache) is False
    assert calls == [], "a worker that loses the claim must not call clairvoyance"


async def test_claim_released_when_state_unknown(route, monkeypatch):
    """A blip must not burn the day's only attempt."""
    monkeypatch.setattr(health_restore, "_past_target_time", lambda: True)
    route(lambda r: httpx.Response(500, text="boom"))
    cache = _cache()
    await health_restore.run_daily_restore(cache)
    assert cache._metadata.claims == [
        (HEALTH_RESTORE_CLAIM, cache._metadata.claims[0][1])
    ]
    assert cache._metadata.releases == [HEALTH_RESTORE_CLAIM]


async def test_claim_kept_when_already_healthy(route, monkeypatch):
    """A settled state is a completed run — do not retry it all day."""
    monkeypatch.setattr(health_restore, "_past_target_time", lambda: True)
    route(lambda r: httpx.Response(200, json={"health": "healthy"}))
    cache = _cache()
    await health_restore.run_daily_restore(cache)
    assert cache._metadata.releases == []


async def test_before_target_time_does_nothing(route, monkeypatch):
    monkeypatch.setattr(health_restore, "_past_target_time", lambda: False)
    calls = route(lambda r: httpx.Response(200, json={"health": "unhealthy"}))
    cache = _cache()
    assert await health_restore.run_daily_restore(cache) is False
    assert calls == []
    assert cache._metadata.claims == [], "must not claim before the target time"


# ---------------------------------------------------------------------------
# configuration gates
# ---------------------------------------------------------------------------


async def test_unconfigured_is_a_clean_noop(route, monkeypatch):
    monkeypatch.setattr(settings, "clairvoyance_jwt_token", "")
    calls = route(lambda r: httpx.Response(200, json={"health": "unhealthy"}))
    assert await health_restore.run_daily_restore(_cache(), force=True) is False
    assert calls == []


async def test_disabled_is_a_clean_noop(route, monkeypatch):
    monkeypatch.setattr(settings, "health_restore_enabled", False)
    calls = route(lambda r: httpx.Response(200, json={"health": "unhealthy"}))
    assert await health_restore.run_daily_restore(_cache(), force=True) is False
    assert calls == []


@pytest.mark.parametrize(
    "value,expected_hour,expected_min",
    [("00:30", 0, 30), ("06:00", 6, 0), ("bogus", 0, 30), ("99:99", 0, 30)],
)
def test_target_time_parsing_falls_back_rather_than_raising(
    monkeypatch, value, expected_hour, expected_min
):
    """A malformed value must not raise on every tick — it falls back to 00:30."""
    monkeypatch.setattr(settings, "health_restore_time_utc", value)
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    target = now.replace(
        hour=expected_hour, minute=expected_min, second=0, microsecond=0
    )
    assert health_restore._past_target_time() == (now >= target)
