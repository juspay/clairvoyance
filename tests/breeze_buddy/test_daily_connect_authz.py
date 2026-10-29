"""N-TEL-2: Daily /connect must enforce tenant ownership on the lead, and must
not let a caller tell "lead does not exist" apart from "exists but not yours"."""

import asyncio
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

import app.api.routers.breeze_buddy.daily.handlers as handlers
from app.schemas import (
    BreezeBuddyDailyConnectRequest,
    ExecutionMode,
    LeadCallStatus,
    LeadCallTracker,
    UserInfo,
    UserRole,
)


def _merchant(
    reseller_ids: List[str], merchant_ids: Optional[List[str]] = None
) -> UserInfo:
    return UserInfo(
        id="u-1",
        username="caller",
        role=UserRole.MERCHANT,
        reseller_ids=reseller_ids,
        merchant_ids=merchant_ids or [],
    )


def _request() -> BreezeBuddyDailyConnectRequest:
    return BreezeBuddyDailyConnectRequest(lead_id="lead-victim")


def _daily_lead(reseller_id: str) -> LeadCallTracker:
    return LeadCallTracker(
        id="lead-victim",
        reseller_id=reseller_id,
        template="t",
        execution_mode=ExecutionMode.DAILY,
        status=LeadCallStatus.BACKLOG,
    )


def test_a_cross_tenant_lead_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """A merchant scoped to one reseller cannot connect to another's lead."""

    async def get_lead_by_id(_lead_id: str) -> Any:
        return _daily_lead("r-victim")

    monkeypatch.setattr(handlers, "get_lead_by_id", get_lead_by_id)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(
            handlers.breeze_buddy_daily_connect_handler(
                _request(), _merchant(reseller_ids=["r-mine"])
            )
        )
    assert ei.value.status_code == 404
    assert ei.value.detail == "Lead not found"


def test_a_missing_lead_returns_the_same_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not-found and access-denied are indistinguishable (no id enumeration)."""

    async def get_lead_by_id(_lead_id: str) -> Any:
        return None

    monkeypatch.setattr(handlers, "get_lead_by_id", get_lead_by_id)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(
            handlers.breeze_buddy_daily_connect_handler(
                _request(), _merchant(reseller_ids=["r-mine"])
            )
        )
    assert ei.value.status_code == 404
    assert ei.value.detail == "Lead not found"


def test_the_leads_own_tenant_may_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller scoped to the lead's reseller is let through to the session."""

    async def get_lead_by_id(_lead_id: str) -> Any:
        return _daily_lead("r-mine")

    async def start_daily_session(_lead_id: str) -> Dict[str, Any]:
        return {"room_url": "x", "token": "y"}

    monkeypatch.setattr(handlers, "get_lead_by_id", get_lead_by_id)
    monkeypatch.setattr(handlers, "start_daily_session", start_daily_session)

    out = asyncio.run(
        handlers.breeze_buddy_daily_connect_handler(
            _request(), _merchant(reseller_ids=["r-mine"])
        )
    )
    assert out["room_url"] == "x"
    assert out["noise_cancellation"] is True
