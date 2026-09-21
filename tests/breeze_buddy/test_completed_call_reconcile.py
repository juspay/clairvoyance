from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

# Import the dispatch package first: managers.calls reaches into
# dispatch.alerts, whose package __init__ imports dispatch.worker, which
# imports managers.calls straight back.
from app.ai.voice.agents.breeze_buddy import dispatch as _dispatch  # noqa: F401
from app.ai.voice.agents.breeze_buddy.managers import calls as calls_mod
from app.schemas import CallDirection, ExecutionMode, LeadCallStatus
from app.schemas.breeze_buddy.core import LeadCallTracker

CALL_SID = "aaa44bfa-32ba-42e4-b5c5-ee1acc6e5392"


def call_sid_of(_kwargs: Dict[str, Any]) -> str:
    """The close is keyed by lead id; tests assert on the call it stands for."""
    return CALL_SID


def make_lead(
    lead_id: str = "lead-1",
    status: LeadCallStatus = LeadCallStatus.PROCESSING,
    outcome: Optional[str] = None,
    meta_data: Optional[Dict[str, Any]] = None,
    is_locked: bool = True,
) -> LeadCallTracker:
    """A dispatched outbound lead: PROCESSING and is_locked, per worker.py."""
    return LeadCallTracker(
        id=lead_id,
        reseller_id="breeze",
        template="flipkart-recovery-luna-normal-v9",
        template_id="tmpl-1",
        merchant_id="flipkart",
        request_id="req-1",
        attempt_count=0,
        next_attempt_at=datetime.now(timezone.utc),
        payload={"customer_mobile_number": "+917736682425"},
        metaData=meta_data,
        status=status,
        outcome=outcome,
        is_locked=is_locked,
        call_id=CALL_SID,
        call_initiated_time=datetime(2026, 9, 21, 15, 4, 26, tzinfo=timezone.utc),
        execution_mode=ExecutionMode.TELEPHONY,
        call_direction=CallDirection.OUTBOUND,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the grace period — the delay's value is not under test."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(calls_mod.asyncio, "sleep", _instant)


class _Harness:
    """Records what the reconcile did, with a claim that obeys the real SQL."""

    def __init__(
        self,
        row: LeadCallTracker,
        claimed_row: Optional[LeadCallTracker] = None,
        raises: bool = False,
    ) -> None:
        self.row = row
        # The row as the claim's RETURNING * would see it — lets a test make
        # the claim disagree with the earlier read.
        self.claimed_row = claimed_row if claimed_row is not None else row
        self.raises = raises
        self.handled: List[str] = []
        self.released: List[str] = []
        self.retried: List[str] = []
        self.resources_freed: List[str] = []
        self.closed: Dict[str, Any] = {}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def get_lead(call_id: str) -> LeadCallTracker:
            return self.row

        async def claim(
            lead_id: str,
            expected_status: Optional[LeadCallStatus] = None,
            force: bool = False,
        ) -> Optional[LeadCallTracker]:
            if expected_status is not None and self.row.status != expected_status:
                return None
            if not force and self.row.is_locked:
                return None
            return self.claimed_row

        async def close(**kwargs: Any) -> Optional[LeadCallTracker]:
            self.closed.update(kwargs)
            self.handled.append(call_sid_of(kwargs))
            if self.raises:
                raise RuntimeError("db went away")
            return self.claimed_row

        async def release(lead_id: str) -> None:
            self.released.append(lead_id)

        async def retry(lead: LeadCallTracker, config: Any, *a: Any) -> None:
            self.retried.append(lead.id)

        async def config(*_a: Any, **_k: Any) -> str:
            return "config"

        async def release_resources(lead: LeadCallTracker) -> None:
            self.resources_freed.append(lead.id)

        async def noop(*_a: Any, **_k: Any) -> None:
            return None

        async def must_not_run(call_id: str) -> None:
            raise AssertionError(
                "handle_unanswered_calls asserts NO_ANSWER and blanks "
                "meta_data; this path must close the row itself"
            )

        monkeypatch.setattr(calls_mod, "get_lead_by_call_id", get_lead)
        monkeypatch.setattr(calls_mod, "acquire_lock_on_lead_by_id", claim)
        monkeypatch.setattr(calls_mod, "update_lead_call_completion_details", close)
        monkeypatch.setattr(calls_mod, "release_lock_on_lead_by_id", release)
        monkeypatch.setattr(calls_mod, "_release_call_resources", release_resources)
        monkeypatch.setattr(calls_mod, "_get_lead_config", config)
        monkeypatch.setattr(calls_mod, "_retry_call", retry)
        monkeypatch.setattr(calls_mod, "handle_unanswered_calls", must_not_run)


# ---------------------------------------------------------------------------
# reconcile_completed_call — the missing writer for ``completed``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dispatched_outbound_lead_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The target population: PROCESSING and is_locked. Needs force=True."""
    h = _Harness(make_lead(is_locked=True))
    h.install(monkeypatch)

    await calls_mod.reconcile_completed_call(CALL_SID)

    assert h.handled == [CALL_SID]
    assert h.released == ["lead-1"]


@pytest.mark.asyncio
async def test_the_close_does_not_assert_a_cause_it_cannot_observe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No outcome is equally a crashed pipeline — say UNKNOWN, keep the notes."""
    h = _Harness(make_lead(meta_data={"playground": True}))
    h.install(monkeypatch)

    await calls_mod.reconcile_completed_call(CALL_SID)

    assert h.closed["outcome"] == "UNKNOWN", "NO_ANSWER asserts an unobserved cause"
    assert h.closed["status"] == LeadCallStatus.FINISHED
    assert h.closed["meta_data"]["playground"] is True, "meta_data must merge"
    assert h.closed["meta_data"]["cleanup"] == "completed_no_pipeline"
    assert h.retried == ["lead-1"], "the customer is still called back"
    assert h.resources_freed == ["lead-1"], "the telephony channel must be freed"


@pytest.mark.asyncio
async def test_completed_is_noop_when_pipeline_already_closed_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Race guard: the webhook must never re-close a finished conversation."""
    h = _Harness(
        make_lead(status=LeadCallStatus.FINISHED, outcome="APP_NUDGE_ACCEPTED")
    )
    h.install(monkeypatch)

    await calls_mod.reconcile_completed_call(CALL_SID)

    assert h.handled == []


@pytest.mark.asyncio
async def test_a_crashed_mid_call_pipeline_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outcome means a conversation happened — never re-dial those."""
    h = _Harness(make_lead(outcome="GUIDANCE_STEP_STARTED"))
    h.install(monkeypatch)

    await calls_mod.reconcile_completed_call(CALL_SID)

    assert h.handled == []
    assert h.released == ["lead-1"], "the claim must not leak the lock"


@pytest.mark.asyncio
async def test_the_outcome_is_read_from_the_claim_not_the_earlier_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outcome landing between the read and the claim must still win."""
    h = _Harness(
        make_lead(outcome=None),
        claimed_row=make_lead(outcome="GUIDANCE_STEP_STARTED"),
    )
    h.install(monkeypatch)

    await calls_mod.reconcile_completed_call(CALL_SID)

    assert h.handled == []


@pytest.mark.asyncio
async def test_a_lost_claim_does_not_retry_a_second_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate callback loses the claim and must not dial again."""
    h = _Harness(make_lead())
    h.install(monkeypatch)

    async def lost(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(calls_mod, "acquire_lock_on_lead_by_id", lost)

    await calls_mod.reconcile_completed_call(CALL_SID)

    assert h.handled == []


@pytest.mark.asyncio
async def test_the_lock_is_released_when_the_close_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stranded lock would hide the row from every later sweep."""
    h = _Harness(make_lead(), raises=True)
    h.install(monkeypatch)

    with pytest.raises(RuntimeError):
        await calls_mod.reconcile_completed_call(CALL_SID)

    assert h.released == ["lead-1"]


@pytest.mark.asyncio
async def test_completed_is_noop_for_orphan_call_sid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No lead for the SID: the owning handlers already alert on orphans."""
    handled: List[str] = []

    async def no_lead(call_id: str) -> None:
        return None

    async def unanswered(call_id: str) -> None:
        handled.append(call_id)

    monkeypatch.setattr(calls_mod, "get_lead_by_call_id", no_lead)
    monkeypatch.setattr(calls_mod, "handle_unanswered_calls", unanswered)

    await calls_mod.reconcile_completed_call(CALL_SID)

    assert handled == []


# ---------------------------------------------------------------------------
# reconcile_stuck_processing_leads — a last resort must not destroy evidence
# ---------------------------------------------------------------------------


async def _run_reaper_capturing_close(
    monkeypatch: pytest.MonkeyPatch, lead: LeadCallTracker
) -> Dict[str, Any]:
    """Drive the reaper over one stale lead, returning its close kwargs."""
    captured: Dict[str, Any] = {}

    async def fake_stale(*_args: Any, **_kwargs: Any) -> List[LeadCallTracker]:
        return [lead]

    async def fake_acquire(*_args: Any, **_kwargs: Any) -> LeadCallTracker:
        return lead

    async def fake_update(**kwargs: Any) -> None:
        captured.update(kwargs)
        return None

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(calls_mod, "get_leads_by_status_and_time_before", fake_stale)
    monkeypatch.setattr(calls_mod, "acquire_lock_on_lead_by_id", fake_acquire)
    monkeypatch.setattr(calls_mod, "update_lead_call_completion_details", fake_update)
    monkeypatch.setattr(calls_mod, "release_lock_on_lead_by_id", noop)
    monkeypatch.setattr(calls_mod, "_release_call_resources", noop)
    monkeypatch.setattr(calls_mod, "_get_lead_config", noop)

    await calls_mod.reconcile_stuck_processing_leads()
    return captured


@pytest.mark.asyncio
async def test_reaper_preserves_mid_call_outcome_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The update REPLACES meta_data, so the cleanup must merge into it."""
    lead = make_lead(
        outcome="GUIDANCE_STEP_STARTED",
        meta_data={
            "outcome": {"committed_step": "app khol liya"},
            "call_ended_by": "customer",
        },
    )

    captured = await _run_reaper_capturing_close(monkeypatch, lead)

    assert captured["outcome"] == "GUIDANCE_STEP_STARTED"
    assert captured["meta_data"]["cleanup"] == "stuck_processing_timeout"
    assert captured["meta_data"]["outcome"] == {"committed_step": "app khol liya"}
    assert captured["meta_data"]["call_ended_by"] == "customer"


@pytest.mark.asyncio
async def test_reaper_still_falls_back_to_unknown_without_an_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserving a real outcome must not leave outcome NULL when none exists."""
    captured = await _run_reaper_capturing_close(monkeypatch, make_lead(outcome=None))

    assert captured["outcome"] == "UNKNOWN"
    assert captured["meta_data"] == {"cleanup": "stuck_processing_timeout"}
