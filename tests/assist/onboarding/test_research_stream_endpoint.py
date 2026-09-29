"""``POST /assist/onboarding/research/stream``: who may ask, how often, what
streams back."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingConfigurationError,
    WebsiteScrapingUnavailableError,
    WebsiteScrapingUpstreamError,
)
from app.ai.voice.agents.breeze_buddy.assist.onboarding.research import stream as runs
from app.api.routers.breeze_buddy.assist.onboarding import research as research_route
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist.onboarding.research import (
    AssistResearchCompletion,
)
from app.services.redis.rate_limit import RateLimitDecision

ADMIN = UserInfo(
    id="admin-1",
    username="admin",
    role=UserRole.ADMIN,
    reseller_ids=["*"],
    merchant_ids=["*"],
)
MERCHANT = UserInfo(
    id="m-1",
    username="merchant",
    role=UserRole.MERCHANT,
    reseller_ids=["BB_ASSIST"],
    merchant_ids=["shop-1"],
)
BODY = {"url": "https://shop.test", "reseller_id": "BB_ASSIST", "merchant_id": "shop-1"}


def _allow(monkeypatch, allowed: bool = True) -> AsyncMock:
    limiter = AsyncMock(
        return_value=RateLimitDecision(
            allowed=allowed,
            count=4 if not allowed else 1,
            limit=3,
            retry_after_seconds=60,
        )
    )
    monkeypatch.setattr(research_route, "check_rate_limit", limiter)
    return limiter


def _research(monkeypatch, *, raises: Exception | None = None) -> AsyncMock:
    async def run(url: str, *, on_event=None, **_: Any) -> AssistResearchCompletion:
        if raises:
            raise raises
        if on_event:
            await on_event("progress", {"detail": "Reading the home page"})
            await on_event(
                "note",
                {
                    "field": "offer_items",
                    "value": "Free shipping",
                    "source_url": f"{url}/faq",
                },
            )
        return AssistResearchCompletion()

    mock = AsyncMock(side_effect=run)
    monkeypatch.setattr(runs.service, "read_facts", mock)
    return mock


def _client(user: UserInfo) -> TestClient:
    app = FastAPI()
    app.include_router(research_route.router)
    app.dependency_overrides[get_current_user_with_rbac] = lambda: user
    return TestClient(app)


def _events(response) -> List[Tuple[str, Dict[str, Any]]]:
    events: List[Tuple[str, Dict[str, Any]]] = []
    assert response.status_code == 200, response.text
    for block in response.text.strip().split("\n\n"):
        lines: Dict[str, str] = {}
        for line in block.splitlines():
            key, value = line.split(": ", 1)
            lines[key] = value
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_a_run_streams_progress_notes_then_done(monkeypatch) -> None:
    _allow(monkeypatch)
    run = _research(monkeypatch)
    response = _client(MERCHANT).post("/assist/onboarding/research/stream", json=BODY)
    events = _events(response)
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "progress"
    assert "note" in kinds
    assert kinds[-1] == "done"
    assert (
        "note",
        {
            "field": "offer_items",
            "value": "Free shipping",
            "source_url": "https://shop.test/faq",
        },
    ) in events
    assert events[-1][1] == {
        "success": True,
        "status": "completed",
    }
    run.assert_called_once()


def test_a_quiet_run_sends_pings(monkeypatch) -> None:
    _allow(monkeypatch)
    monkeypatch.setattr(runs, "PING_SECONDS", 0.01)

    async def slow(url: str, **_: Any) -> AssistResearchCompletion:
        await asyncio.sleep(0.1)
        return AssistResearchCompletion()

    monkeypatch.setattr(runs.service, "read_facts", slow)
    events = _events(
        _client(ADMIN).post("/assist/onboarding/research/stream", json=BODY)
    )
    assert ("ping", {}) in events
    assert events[-1][0] == "done"


@pytest.mark.parametrize(
    "error, message, retryable",
    [
        (WebsiteScrapingUpstreamError("no page"), "We could not read", False),
        (WebsiteScrapingConfigurationError("no key"), "not available", False),
        (WebsiteScrapingUnavailableError("402"), "not available", True),
        (RuntimeError("boom"), "could not be completed", True),
    ],
)
def test_failures_end_in_one_error_event(
    monkeypatch, error, message, retryable
) -> None:
    _allow(monkeypatch)
    _research(monkeypatch, raises=error)
    events = _events(
        _client(ADMIN).post("/assist/onboarding/research/stream", json=BODY)
    )
    kind, data = events[-1]
    assert kind == "error"
    assert message in data["message"]
    assert data["retryable"] is retryable
    assert data["success"] is False
    assert "boom" not in data["message"]
    assert not any(k == "done" for k, _ in events)


def test_the_limit_is_counted_per_user(monkeypatch) -> None:
    limiter = _allow(monkeypatch)
    _research(monkeypatch)
    _client(MERCHANT).post("/assist/onboarding/research/stream", json=BODY)
    limiter.assert_awaited_once()
    call = limiter.call_args
    assert (call.kwargs["bucket"], call.kwargs["identifier"]) == (
        "assist_research_user",
        "m-1",
    )
    assert call.kwargs["limit"] == 10
    assert call.kwargs["window_seconds"] == 86400
    assert call.kwargs["fail_closed"]


def test_a_caller_at_the_cap_gets_429_and_no_run(monkeypatch) -> None:
    _allow(monkeypatch, allowed=False)
    run = _research(monkeypatch)
    response = _client(MERCHANT).post("/assist/onboarding/research/stream", json=BODY)
    assert response.status_code == 429
    run.assert_not_called()


@pytest.mark.parametrize(
    "change, code",
    [
        ({"merchant_id": None}, 403),
        ({"merchant_id": "someone-else"}, 403),
        ({"url": "http://localhost/"}, 400),
    ],
    ids=["no-merchant", "other-merchant", "unsafe-url"],
)
def test_bad_requests_are_refused_before_any_research(
    monkeypatch, change, code
) -> None:
    _allow(monkeypatch)
    run = _research(monkeypatch)
    body = {k: v for k, v in {**BODY, **change}.items() if v is not None}
    assert (
        _client(MERCHANT)
        .post("/assist/onboarding/research/stream", json=body)
        .status_code
        == code
    )
    run.assert_not_called()
