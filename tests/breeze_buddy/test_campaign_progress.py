"""
Campaign progress correctness: `dialed` and per-lead attempt counts.

The stored lead_call_tracker.attempt_count is a 0-BASED retry counter (a
lead answered on its first attempt finishes with 0), so anything that
means "attempts made" must add 1 when a call was actually initiated —
and never for leads that finished without a dial (ABORT).
"""

from datetime import datetime, timezone
from typing import Any, Dict

from app.api.routers.breeze_buddy.analytics.handlers import (
    _build_call_detail_result,
)
from app.database.queries.breeze_buddy.campaigns import (
    campaign_stats_query,
)

_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def _tracker(**over: Any) -> Dict[str, Any]:
    base = {
        "id": "lead-1",
        "call_id": "call-1",
        "request_id": None,
        "template": "t",
        "reseller_id": "rs",
        "merchant_id": None,
        "status": "FINISHED",
        "outcome": "INTERESTED",
        "recording_url": None,
        "calling_provider": None,
        "attempt_count": 0,
        "cost": None,
        "payload": None,
        "meta_data": None,
        "call_initiated_time": _NOW,
        "created_at": _NOW,
        "updated_at": _NOW,
        "execution_mode": "TELEPHONY",
        "call_direction": "OUTBOUND",
    }
    base.update(over)
    return base


def test_first_attempt_connect_counts_as_one_attempt():
    r = _build_call_detail_result(_tracker(attempt_count=0))
    assert r.attempt_count == 1


def test_retried_lead_counts_stored_retries_plus_current():
    r = _build_call_detail_result(_tracker(attempt_count=1))
    assert r.attempt_count == 2


def test_never_dialed_abort_shows_zero_attempts():
    r = _build_call_detail_result(
        _tracker(attempt_count=0, call_initiated_time=None, outcome="ABORT")
    )
    assert r.attempt_count == 0


def test_dialed_predicate_covers_first_attempt_connects():
    # Regression guard for "Completed campaign shows 0 / N dialed": the
    # aggregate must not rely on attempt_count alone.
    q, _ = campaign_stats_query(["c1"])
    assert "attempt_count > 0 OR call_initiated_time IS NOT NULL" in q
