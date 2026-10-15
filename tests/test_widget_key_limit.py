"""The per-widget cap across all visitors (PT-18).

Each action gets (per-visitor hourly limit x WIDGET_KEY_LIMIT_MULTIPLIER)
requests per short window for one widget, keyed on the widget alone so rotating
IPs cannot escape it, and clearing within a minute so a burst does not block a
store's real shoppers for an hour.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException, Request

from app.api.routers.breeze_buddy import widget_common
from app.services.redis.rate_limit import RateLimitDecision


def _req() -> Request:
    return cast(
        Request,
        SimpleNamespace(
            headers={"x-forwarded-for": "49.37.12.5, 34.111.84.202"},
            client=SimpleNamespace(host="10.0.0.1"),
        ),
    )


@pytest.fixture
def calls(monkeypatch):
    seen: list = []

    async def _check(*, bucket, identifier, limit, window_seconds, prefix, **kw):
        seen.append(
            dict(
                bucket=bucket, identifier=identifier, limit=limit, window=window_seconds
            )
        )
        allowed = not (bucket.endswith(":agg") and limit == 1)
        return RateLimitDecision(
            allowed=allowed, count=2, limit=limit, retry_after_seconds=42
        )

    monkeypatch.setattr(widget_common, "check_rate_limit", _check)
    return seen


async def test_widget_cap_is_per_minute_and_scaled_from_the_visitor_limit(calls):
    await widget_common.enforce_widget_ip_limit(
        request=_req(), bucket="message", limit=600, widget_config_id="w1"
    )
    per_visitor, per_widget = calls
    assert per_visitor["window"] == 3600 and per_visitor["limit"] == 600
    assert per_widget == dict(
        bucket="message:agg", identifier="w1", limit=6000, window=60
    )


async def test_widget_cap_is_keyed_on_the_widget_not_the_visitor(calls):
    await widget_common.enforce_widget_ip_limit(
        request=_req(), bucket="voice_connect", limit=10, widget_config_id="w1"
    )
    assert "49.37.12.5" not in calls[1]["identifier"]
    assert calls[1]["identifier"] == "w1"


async def test_tripped_widget_cap_says_when_to_retry(calls, monkeypatch):
    monkeypatch.setattr(widget_common, "WIDGET_KEY_LIMIT_MULTIPLIER", 1)
    with pytest.raises(HTTPException) as e:
        await widget_common.enforce_widget_ip_limit(
            request=_req(), bucket="session_create", limit=1, widget_config_id="w1"
        )
    assert e.value.status_code == 429
    assert (e.value.headers or {}).get("Retry-After") == "42"
    assert "try again in 42 seconds" in e.value.detail


async def test_multiplier_zero_disables_the_widget_cap(monkeypatch):
    # The real limiter treats a non-positive limit as "disabled".
    from app.services.redis import rate_limit as svc

    monkeypatch.setattr(svc, "is_redis_configured", lambda: True)
    monkeypatch.setattr(widget_common, "WIDGET_KEY_LIMIT_MULTIPLIER", 0)
    decision = await svc.check_rate_limit(
        bucket="message:agg",
        identifier="w1",
        limit=600 * widget_common.WIDGET_KEY_LIMIT_MULTIPLIER,
        window_seconds=60,
    )
    assert decision.allowed
