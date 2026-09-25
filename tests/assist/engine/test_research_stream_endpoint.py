"""``POST /assist/research/stream``: who may ask, how often, what streams back."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from app.ai.voice.agents.breeze_buddy.assist.engine.research import (
    agent,
    runs,
    tools,
)
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    EgressNotGuardedError,
    FetchFailedError,
)
from app.api.routers.breeze_buddy.assist import research
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist.research import SiteResearchRequest
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
    async def run(url: str, *, on_event=None, **_: Any) -> agent.ResearchResult:
        if raises:
            raise raises
        evidence = tools.Evidence(root=url)
        evidence.add(
            tools.Page(
                url=f"{url}/faq",
                status=200,
                content_type="text/html",
                text="x",
                size_bytes=1,
            )
        )
        evidence.note("offer_items", "Free shipping", f"{url}/faq")
        if on_event:
            await on_event(
                "note",
                {
                    "field": "offer_items",
                    "value": "Free shipping",
                    "source_url": f"{url}/faq",
                },
            )
        return agent.ResearchResult(evidence=evidence, steps_used=3)

    mock = AsyncMock(side_effect=run)
    monkeypatch.setattr(research.runs.agent, "research", mock)
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


def test_a_run_streams_progress_notes_then_done(monkeypatch) -> None:
    _allow(monkeypatch)
    _research(monkeypatch)
    response = _client(MERCHANT).post("/assist/research/stream", json=BODY)
    assert response.status_code == 200
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
    }


@pytest.mark.parametrize(
    "error, code, retryable",
    [
        (FetchFailedError("refused"), "unreadable_site", False),
        (EgressNotGuardedError("proxy"), "unavailable", False),
        (agent.WebsiteScrapingConfigurationError("no key"), "unavailable", False),
        (RuntimeError("boom"), "research_failed", True),
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


def test_the_daily_limit_refuses_before_any_research(monkeypatch) -> None:
    _allow(monkeypatch, allowed=False)
    run = _research(monkeypatch)
    response = _client(MERCHANT).post("/assist/research/stream", json=BODY)
    assert response.status_code == 429
    run.assert_not_called()


def test_the_limit_is_counted_per_user_and_per_merchant(monkeypatch) -> None:
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


def test_a_merchant_at_its_cap_does_not_spend_the_callers_runs(monkeypatch) -> None:
    limiter = _allow(monkeypatch, allowed=False)
    run = _research(monkeypatch)
    response = _client(MERCHANT).post("/assist/research/stream", json=BODY)
    assert response.status_code == 429
    assert [c.kwargs["bucket"] for c in limiter.call_args_list] == [
        "assist_research_merchant"
    ]
    run.assert_not_called()


def test_leaving_out_the_merchant_still_counts_the_user(monkeypatch) -> None:
    limiter = _allow(monkeypatch)
    _research(monkeypatch)
    body = {k: v for k, v in BODY.items() if k != "merchant_id"}
    _client(ADMIN).post("/assist/research/stream", json=body)
    assert [c.kwargs["bucket"] for c in limiter.call_args_list] == [
        "assist_research_user"
    ]


def test_a_merchant_must_name_its_merchant(monkeypatch) -> None:
    _allow(monkeypatch)
    run = _research(monkeypatch)
    body = {k: v for k, v in BODY.items() if k != "merchant_id"}
    assert (
        _client(MERCHANT).post("/assist/research/stream", json=body).status_code == 400
    )
    run.assert_not_called()


def test_another_merchants_store_is_refused(monkeypatch) -> None:
    _allow(monkeypatch)
    run = _research(monkeypatch)
    body = {**BODY, "merchant_id": "someone-else"}
    assert (
        _client(MERCHANT).post("/assist/research/stream", json=body).status_code == 403
    )
    run.assert_not_called()


def test_an_unsafe_url_is_refused_before_any_research(monkeypatch) -> None:
    _allow(monkeypatch)
    run = _research(monkeypatch)
    body = {**BODY, "url": "http://localhost/"}
    assert _client(ADMIN).post("/assist/research/stream", json=body).status_code == 400
    run.assert_not_called()


def _slots(monkeypatch, **held: int) -> runs.RunSlots:
    slots = runs.RunSlots(total=4, per_user=2)
    for user_id, count in held.items():
        for _ in range(count):
            slots.claim(user_id)
    monkeypatch.setattr(research, "_slots", slots)
    return slots


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


def test_a_run_holds_its_slot_only_while_it_streams(monkeypatch) -> None:
    _allow(monkeypatch)
    slots = _slots(monkeypatch)
    seen: List[Dict[str, int]] = []

    async def run(url: str, *, on_event=None, **_: Any) -> agent.ResearchResult:
        seen.append(slots.held())
        return agent.ResearchResult(evidence=tools.Evidence(root=url), steps_used=1)

    monkeypatch.setattr(research.runs.agent, "research", AsyncMock(side_effect=run))
    events = _events(_client(MERCHANT).post("/assist/research/stream", json=BODY))
    assert seen == [{"m-1": 1}]
    assert events[-1][0] == "done"
    assert slots.held() == {}


def test_a_daily_limit_refusal_gives_the_slot_back(monkeypatch) -> None:
    _allow(monkeypatch, allowed=False)
    slots = _slots(monkeypatch)
    response = _client(MERCHANT).post("/assist/research/stream", json=BODY)
    assert response.status_code == 429
    assert slots.held() == {}


async def test_requests_arriving_together_spend_only_the_runs_that_start(
    monkeypatch,
) -> None:
    async def slow_limiter(**_: Any) -> RateLimitDecision:
        await asyncio.sleep(0)  # Redis round trip: other requests run here
        return RateLimitDecision(allowed=True, count=1, limit=3, retry_after_seconds=60)

    limiter = AsyncMock(side_effect=slow_limiter)
    monkeypatch.setattr(research, "check_rate_limit", limiter)
    slots = _slots(monkeypatch)
    request = SiteResearchRequest(**BODY)

    results = await asyncio.gather(
        *(
            research.research_site_stream(request, current_user=MERCHANT)
            for _ in range(4)
        ),
        return_exceptions=True,
    )

    started = [r for r in results if isinstance(r, StreamingResponse)]
    refused = [r for r in results if isinstance(r, HTTPException)]
    assert len(started) == 2
    assert [r.status_code for r in refused] == [429, 429]
    # user + merchant bucket, for the two that started only
    assert limiter.call_count == 4
    assert slots.held() == {"m-1": 2}


async def _serve(response, send) -> None:
    async def receive() -> Dict[str, Any]:
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
    await response(scope, receive, send)


async def test_a_response_that_never_sends_gives_the_slot_back(monkeypatch) -> None:
    _allow(monkeypatch)
    run = _research(monkeypatch)
    slots = _slots(monkeypatch)
    response = await research.research_site_stream(
        SiteResearchRequest(**BODY), current_user=MERCHANT
    )

    async def send(_: Dict[str, Any]) -> None:
        raise OSError("client gone")

    with pytest.raises(ClientDisconnect):
        await _serve(response, send)
    assert slots.held() == {}
    run.assert_not_called()


async def test_a_client_leaving_mid_stream_stops_the_run(monkeypatch) -> None:
    _allow(monkeypatch)
    slots = _slots(monkeypatch)
    stopped = asyncio.Event()

    async def run(url: str, *, on_event=None, **_: Any) -> agent.ResearchResult:
        try:
            await asyncio.sleep(3600)
        finally:
            stopped.set()
        raise AssertionError("unreachable")

    monkeypatch.setattr(research.runs.agent, "research", AsyncMock(side_effect=run))
    response = await research.research_site_stream(
        SiteResearchRequest(**BODY), current_user=MERCHANT
    )
    sent: List[Dict[str, Any]] = []

    async def send(message: Dict[str, Any]) -> None:
        sent.append(message)
        if message.get("body"):
            raise OSError("client gone")

    with pytest.raises(ClientDisconnect):
        await _serve(response, send)
    await asyncio.wait_for(stopped.wait(), 1)
    assert slots.held() == {}
