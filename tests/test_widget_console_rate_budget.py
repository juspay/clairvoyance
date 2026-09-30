"""Console preview traffic is counted separately from a merchant's shoppers.

Reloading a preview opens a session. A merchant iterating on their greeting
does that a dozen times a minute, and before this that spent the very cap
meant to bound anonymous visitors on their storefront — they could rate-limit
themselves out of their own widget by editing it.

The fix is a separate bucket with its own cap, NOT an exemption: the widget
endpoints cost LLM calls and Origin is a browser control, so "no limit when
you claim to be the console" would be free traffic for anyone holding a
public widget key (which ships in every embed snippet).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.api.routers.breeze_buddy import widget_common
from app.core.config.static import WIDGET_CONSOLE_RATE_LIMIT_PER_HOUR
from app.services.redis.rate_limit import RateLimitDecision

MERCHANT_LIMIT = 20
WIDGET_CONFIG_ID = "00000000-0000-0000-0000-0000000000aa"


def _request(origin: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/agent/voice/breeze-buddy/chat/widget/session",
            "headers": [(b"origin", origin.encode())],
            "client": ("203.0.113.9", 51234),
            "query_string": b"",
            "scheme": "https",
            "server": ("clairvoyance.test", 443),
        }
    )


@pytest.fixture()
def limiter(monkeypatch) -> AsyncMock:
    mock = AsyncMock(
        return_value=RateLimitDecision(
            allowed=True, count=1, limit=MERCHANT_LIMIT, retry_after_seconds=0
        )
    )
    monkeypatch.setattr(widget_common, "check_rate_limit", mock)
    return mock


async def _kwargs(limiter: AsyncMock, origin: str) -> dict:
    """What the limiter was actually asked for, once."""
    await _enforce(origin)
    call = limiter.await_args
    assert call is not None, "the limiter was never called"
    return dict(call.kwargs)


async def _enforce(origin: str) -> None:
    await widget_common.enforce_widget_ip_limit(
        request=_request(origin),
        bucket="chat_session",
        limit=MERCHANT_LIMIT,
        widget_config_id=WIDGET_CONFIG_ID,
    )


@pytest.mark.asyncio
async def test_a_shopper_spends_the_merchants_budget(limiter: AsyncMock) -> None:
    kwargs = await _kwargs(limiter, "https://zodiaconline.com")
    assert kwargs["bucket"] == "chat_session"
    assert kwargs["limit"] == MERCHANT_LIMIT


@pytest.mark.asyncio
async def test_the_console_spends_its_own(limiter: AsyncMock) -> None:
    kwargs = await _kwargs(limiter, "https://breezebuddy.ai")
    assert kwargs["bucket"] == "console_chat_session"
    assert kwargs["limit"] == WIDGET_CONSOLE_RATE_LIMIT_PER_HOUR
    # Same merchant scoping either way, so one console user cannot drain
    # another merchant's counter.
    assert kwargs["identifier"].startswith(f"{WIDGET_CONFIG_ID}:")


@pytest.mark.asyncio
async def test_the_console_is_still_bounded(monkeypatch) -> None:
    # Not an exemption: over its own cap, the console gets the same 429.
    monkeypatch.setattr(
        widget_common,
        "check_rate_limit",
        AsyncMock(
            return_value=RateLimitDecision(
                allowed=False,
                count=WIDGET_CONSOLE_RATE_LIMIT_PER_HOUR,
                limit=WIDGET_CONSOLE_RATE_LIMIT_PER_HOUR,
                retry_after_seconds=42,
            )
        ),
    )
    with pytest.raises(Exception) as caught:
        await _enforce("https://breezebuddy.ai")
    assert getattr(caught.value, "status_code", None) == 429
