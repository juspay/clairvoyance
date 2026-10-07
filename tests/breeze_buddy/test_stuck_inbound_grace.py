"""The stuck-call sweep gives an inbound call a longer grace than an outbound one.

An outbound lead still PROCESSING after BB_STUCK_CALL_STALE_MINUTES is wedged. An
inbound one of the same age is usually a customer still talking, and the sweep frees
its line, so it must wait BB_INBOUND_STUCK_LEAD_MINUTES before it is closed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

import pytest

# Import the dispatch package first: managers.calls reaches into
# dispatch.alerts, whose package __init__ imports dispatch.worker, which
# imports managers.calls straight back.
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.schemas import CallDirection, ExecutionMode, LeadCallStatus
from app.schemas.breeze_buddy.core import LeadCallTracker

STALE_MINUTES = 30
INBOUND_MINUTES = 240


def make_lead(
    lead_id: str,
    direction: CallDirection,
    started: Optional[datetime],
) -> LeadCallTracker:
    return LeadCallTracker(
        id=lead_id,
        reseller_id="breeze",
        template="support-inbound",
        template_id="tmpl-1",
        merchant_id="merchant-1",
        request_id="req-1",
        attempt_count=0,
        next_attempt_at=datetime.now(timezone.utc),
        payload={"customer_mobile_number": "+919999999999"},
        status=LeadCallStatus.PROCESSING,
        is_locked=True,
        call_id=f"call-{lead_id}",
        call_initiated_time=started,
        execution_mode=ExecutionMode.TELEPHONY,
        call_direction=direction,
    )


def minutes_ago(minutes: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


async def swept_ids(
    monkeypatch: pytest.MonkeyPatch, leads: List[LeadCallTracker]
) -> List[str]:
    """Run the sweep over ``leads`` (what the DB returned as stale) and return the
    ids it closed."""
    closed: List[str] = []
    by_id = {lead.id: lead for lead in leads}

    async def fake_stale(*_args: Any, **_kwargs: Any) -> List[LeadCallTracker]:
        return list(leads)

    async def fake_acquire(lead_id: str, **_kwargs: Any) -> LeadCallTracker:
        return by_id[lead_id]

    async def fake_update(**kwargs: Any) -> None:
        closed.append(kwargs["id"])

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(calls_mod, "BB_STUCK_CALL_STALE_MINUTES", STALE_MINUTES)
    monkeypatch.setattr(calls_mod, "BB_INBOUND_STUCK_LEAD_MINUTES", INBOUND_MINUTES)
    monkeypatch.setattr(calls_mod, "get_leads_by_status_and_time_before", fake_stale)
    monkeypatch.setattr(calls_mod, "acquire_lock_on_lead_by_id", fake_acquire)
    monkeypatch.setattr(calls_mod, "update_lead_call_completion_details", fake_update)
    monkeypatch.setattr(calls_mod, "release_lock_on_lead_by_id", noop)
    monkeypatch.setattr(calls_mod, "_release_call_resources", noop)
    monkeypatch.setattr(calls_mod, "_get_lead_config", noop)
    monkeypatch.setattr(calls_mod, "raise_long_running_call", noop)

    await calls_mod.reconcile_stuck_processing_leads()
    return closed


@pytest.mark.asyncio
async def test_a_40_minute_inbound_call_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the outbound window, inside the inbound one: the customer is probably
    still talking, and closing the lead would free the line mid-call."""
    lead = make_lead("in-40", CallDirection.INBOUND, minutes_ago(40))

    assert await swept_ids(monkeypatch, [lead]) == []


@pytest.mark.asyncio
async def test_a_300_minute_inbound_call_is_swept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the inbound window too: the grace must end, or a lost call-end webhook
    would hold the line for ever."""
    lead = make_lead("in-300", CallDirection.INBOUND, minutes_ago(300))

    assert await swept_ids(monkeypatch, [lead]) == ["in-300"]


@pytest.mark.asyncio
async def test_a_40_minute_outbound_call_is_still_swept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inbound grace must not reach outbound leads."""
    lead = make_lead("out-40", CallDirection.OUTBOUND, minutes_ago(40))

    assert await swept_ids(monkeypatch, [lead]) == ["out-40"]
