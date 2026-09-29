"""``POST /assist/research/stream``: who may ask, how often, what streams back."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, MutableMapping, Tuple
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from app.ai.voice.agents.breeze_buddy.assist.engine.research import site
from app.ai.voice.agents.breeze_buddy.assist.engine.research.exceptions import (
    WebsiteScrapingConfigurationError,
    WebsiteScrapingUpstreamError,
)
from app.ai.voice.agents.breeze_buddy.assist.onboarding import research as runs
from app.api.routers.breeze_buddy.assist import research
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist.probe import ProbeRequest
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
    monkeypatch.setattr(research, "check_rate_limit", limiter)
    return limiter


def _research(monkeypatch, *, raises: Exception | None = None) -> AsyncMock:
    async def run(url: str, *, on_event=None, **_: Any) -> site.ResearchResult:
        if raises:
            raise raises
        note = site.Note("offer_items", "Free shipping", f"{url}/faq")
        if on_event:
            await on_event(
                "note",
                {
                    "field": "offer_items",
                    "value": "Free shipping",
                    "source_url": f"{url}/faq",
                },
            )
        return site.ResearchResult(notes=[note], pages_read=1)

    mock = AsyncMock(side_effect=run)
    monkeypatch.setattr(runs.site, "research", mock)
    return mock


def _client(user: UserInfo) -> TestClient:
    app = FastAPI()
    app.include_router(research.router)
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


def _slots(monkeypatch, **held: int) -> runs.RunSlots:
    slots = runs.RunSlots(total=4, per_user=2)
    for user_id, count in held.items():
        for _ in range(count):
            slots.claim(user_id)
    monkeypatch.setattr(research, "_slots", slots)
    return slots


def test_a_run_streams_progress_notes_then_done(monkeypatch) -> None:
    _allow(monkeypatch)
    run = _research(monkeypatch)
    slots = _slots(monkeypatch)
    response = _client(MERCHANT).post("/assist/research/stream", json=BODY)
    events = _events(response)
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "progress"
    assert "note" in kinds
    assert kinds[-1] == "done"
    assert events[-1][1] == {
        "notes": [
            {
                "field": "offer_items",
                "value": "Free shipping",
                "source_url": "https://shop.test/faq",
            }
        ],
        "pages_read": 1,
        "stopped_because": "finished",
    }
    run.assert_called_once()
    assert slots.held() == {}


def test_a_quiet_run_sends_pings(monkeypatch) -> None:
    _allow(monkeypatch)
    _slots(monkeypatch)
    monkeypatch.setattr(runs, "PING_SECONDS", 0.01)

    async def slow(url: str, **_: Any) -> site.ResearchResult:
        await asyncio.sleep(0.1)
        return site.ResearchResult(pages_read=1)

    monkeypatch.setattr(runs.site, "research", slow)
    events = _events(_client(ADMIN).post("/assist/research/stream", json=BODY))
    assert ("ping", {}) in events
    assert events[-1][0] == "done"


@pytest.mark.parametrize(
    "error, code, retryable",
    [
        (WebsiteScrapingUpstreamError("no page"), "UNREADABLE_SITE", False),
        (WebsiteScrapingConfigurationError("no key"), "RESEARCH_UNAVAILABLE", False),
        (RuntimeError("boom"), "RESEARCH_FAILED", True),
    ],
)
def test_failures_end_in_one_error_event(monkeypatch, error, code, retryable) -> None:
    _allow(monkeypatch)
    _research(monkeypatch, raises=error)
    events = _events(_client(ADMIN).post("/assist/research/stream", json=BODY))
    kind, data = events[-1]
    assert kind == "error"
    assert (data["code"], data["retryable"]) == (code, retryable)
    assert "boom" not in data["message"]
    assert not any(k == "done" for k, _ in events)


def test_the_limit_is_counted_per_merchant_then_per_user(monkeypatch) -> None:
    limiter = _allow(monkeypatch)
    _research(monkeypatch)
    _client(MERCHANT).post("/assist/research/stream", json=BODY)
    counted = [
        (c.kwargs["bucket"], c.kwargs["identifier"], c.kwargs["limit"])
        for c in limiter.call_args_list
    ]
    assert counted == [
        ("assist_research_merchant", "BB_ASSIST:shop-1", 3),
        ("assist_research_user", "m-1", 10),
    ]
    assert all(c.kwargs["window_seconds"] == 86400 for c in limiter.call_args_list)
    assert all(c.kwargs["fail_closed"] for c in limiter.call_args_list)


def test_a_merchant_at_its_cap_spends_nothing_else(monkeypatch) -> None:
    limiter = _allow(monkeypatch, allowed=False)
    run = _research(monkeypatch)
    slots = _slots(monkeypatch)
    response = _client(MERCHANT).post("/assist/research/stream", json=BODY)
    assert response.status_code == 429
    assert [c.kwargs["bucket"] for c in limiter.call_args_list] == [
        "assist_research_merchant"
    ]
    run.assert_not_called()
    assert slots.held() == {}


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
        _client(MERCHANT).post("/assist/research/stream", json=body).status_code == code
    )
    run.assert_not_called()


@pytest.mark.parametrize(
    "held, code",
    [({"a": 2, "b": 2}, 503), ({"m-1": 2}, 429)],
)
def test_too_many_runs_at_once_are_refused_before_the_daily_count(
    monkeypatch, held, code
) -> None:
    limiter = _allow(monkeypatch)
    run = _research(monkeypatch)
    _slots(monkeypatch, **held)
    response = _client(MERCHANT).post("/assist/research/stream", json=BODY)
    assert response.status_code == code
    assert response.headers["Retry-After"] == "30"
    limiter.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("fails_on", ["start", "body"])
async def test_a_client_that_goes_away_frees_the_slot_and_stops_the_run(
    monkeypatch, fails_on
) -> None:
    _allow(monkeypatch)
    slots = _slots(monkeypatch)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def run(url: str, *, on_event=None, **_: Any) -> site.ResearchResult:
        started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            stopped.set()
        raise AssertionError("unreachable")

    monkeypatch.setattr(runs.site, "research", AsyncMock(side_effect=run))
    response = await research.research_site_stream(
        ProbeRequest(**BODY), current_user=MERCHANT
    )

    async def send(message: MutableMapping[str, Any]) -> None:
        if fails_on == "start" or message.get("body"):
            raise OSError("client gone")

    async def receive() -> Dict[str, Any]:
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    if fails_on == "body":
        await asyncio.wait_for(stopped.wait(), 1)
    else:
        assert not started.is_set()
    assert slots.held() == {}
