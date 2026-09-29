"""call.completed is sent once, after the call's own lifecycle is done, and
the stuck sweep hangs a live call up instead of closing it as UNKNOWN."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

import app.ai.voice.agents.breeze_buddy.crm_mirror as crm_mirror

# Import the dispatch package first: managers.calls reaches into
# dispatch.alerts, whose package __init__ imports dispatch.worker, which
# imports managers.calls straight back.
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.schemas import CallDirection, ExecutionMode, LeadCallStatus
from app.schemas.breeze_buddy.core import LeadCallTracker

CALL_SID = "aaa44bfa-32ba-42e4-b5c5-ee1acc6e5392"


def make_lead(**overrides: Any) -> LeadCallTracker:
    fields: Dict[str, Any] = dict(
        id="lead-1",
        reseller_id="breeze",
        template="t",
        template_id="tmpl-1",
        merchant_id="m1",
        payload={"customer_mobile_number": "+917736682425"},
        metaData={},
        status=LeadCallStatus.PROCESSING,
        call_id=CALL_SID,
        telephony_number_id="num-1",
        call_initiated_time=datetime.now(timezone.utc)
        - timedelta(minutes=20, seconds=30),
        execution_mode=ExecutionMode.TELEPHONY,
        call_direction=CallDirection.OUTBOUND,
    )
    fields.update(overrides)
    return LeadCallTracker(**fields)


# ---------------------------------------------------------------------------
# call.completed waits for end_conversation to release it
# ---------------------------------------------------------------------------


def test_a_held_finish_waits_and_an_unheld_one_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: List[str] = []
    monkeypatch.setattr(
        crm_mirror, "mirror_call_completed", lambda lead: sent.append(lead.id)
    )

    held: List[LeadCallTracker] = []
    token = crm_mirror.held_call_completed.set(held)
    crm_mirror._finished_lead_tap(make_lead(id="held"))
    crm_mirror.held_call_completed.reset(token)
    assert sent == [] and [lead.id for lead in held] == ["held"]

    crm_mirror._finished_lead_tap(make_lead(id="unheld"))
    assert sent == ["unheld"]


# ---------------------------------------------------------------------------
# The stuck sweep hangs up before it closes
# ---------------------------------------------------------------------------


class _Sweep:
    def __init__(self, lead: LeadCallTracker, can_hang_up: bool = True) -> None:
        self.lead = lead
        self.can_hang_up = can_hang_up
        self.hung_up: List[str] = []
        self.closed: List[Dict[str, Any]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sweep = self

        async def stale(*_a: Any, **_k: Any) -> List[LeadCallTracker]:
            return [sweep.lead]

        async def claim(*_a: Any, **_k: Any) -> LeadCallTracker:
            return sweep.lead

        async def close(**kw: Any) -> LeadCallTracker:
            sweep.closed.append(kw)
            return sweep.lead

        async def number(_id: str) -> Any:
            return SimpleNamespace(provider="PLIVO")

        class _Provider:
            async def hang_up(self, call_id: str) -> bool:
                sweep.hung_up.append(call_id)
                return sweep.can_hang_up

        async def noop(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr(calls_mod, "get_leads_by_status_and_time_before", stale)
        monkeypatch.setattr(calls_mod, "acquire_lock_on_lead_by_id", claim)
        monkeypatch.setattr(calls_mod, "release_lock_on_lead_by_id", noop)
        monkeypatch.setattr(calls_mod, "update_lead_call_completion_details", close)
        monkeypatch.setattr(calls_mod, "get_telephony_number_by_id", number)
        monkeypatch.setattr(calls_mod, "get_voice_provider", lambda *_a: _Provider())
        monkeypatch.setattr(calls_mod, "_release_call_resources", noop)
        monkeypatch.setattr(calls_mod, "_get_lead_config", noop)


@pytest.mark.asyncio
async def test_just_past_the_threshold_the_call_is_hung_up_not_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live call's pipeline finishes the lead with its real outcome — the
    sweep must not have written UNKNOWN (and call.completed) first."""
    s = _Sweep(make_lead())
    s.install(monkeypatch)

    await calls_mod.reconcile_stuck_processing_leads()

    assert s.hung_up == [CALL_SID]
    assert s.closed == []


@pytest.mark.asyncio
async def test_still_processing_after_the_hang_up_window_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _Sweep(
        make_lead(
            call_initiated_time=datetime.now(timezone.utc) - timedelta(minutes=25)
        )
    )
    s.install(monkeypatch)

    await calls_mod.reconcile_stuck_processing_leads()

    assert s.hung_up == []
    (close,) = s.closed
    assert close["outcome"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_a_provider_that_cannot_hang_up_is_closed_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _Sweep(make_lead(), can_hang_up=False)
    s.install(monkeypatch)

    await calls_mod.reconcile_stuck_processing_leads()

    assert len(s.closed) == 1
