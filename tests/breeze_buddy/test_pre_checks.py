"""Unit tests for the ``recent_contact_cooldown`` internal pre-check function
and its backing count query.

All DB access is mocked at the accessor boundary (``functions`` imports
``count_recent_contacted_leads`` directly, so we patch it in the functions
module namespace). Query-builder tests compile the SQL text only — no DB
round-trip, matching the repo's "verify control flow, not backend semantics"
convention for dispatch tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest

from app.ai.voice.agents.breeze_buddy.managers.pre_checks import functions as fc
from app.database.queries.breeze_buddy.lead_call_tracker import (
    count_recent_contacted_leads_query,
)
from app.schemas import ExecutionMode, LeadCallStatus
from app.schemas.breeze_buddy.core import LeadCallTracker

WINDOW_START = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_ctx(
    lead_id: str = "lead-self",
    request_id: Optional[str] = "req-1",
    merchant_id: Optional[str] = "merchant-1",
    phone: Optional[str] = "+15551234567",
    args: Optional[Dict[str, Any]] = None,
) -> fc.PreCheckFunctionContext:
    """Build a minimal function context — same shape the executor passes."""
    lead = LeadCallTracker(
        id=lead_id,
        reseller_id="res-1",
        template="welcome",
        template_id="tmpl-1",
        merchant_id=merchant_id,
        request_id=request_id,
        attempt_count=0,
        next_attempt_at=datetime.now(timezone.utc),
        payload={"customer_mobile_number": phone} if phone else {},
        status=LeadCallStatus.BACKLOG,
        execution_mode=ExecutionMode.TELEPHONY,
    )
    return fc.PreCheckFunctionContext(
        lead=lead,
        template=None,
        customer_mobile_number=phone,
        merchant_id=merchant_id,
        reseller_id="res-1",
        args=args or {},
        payload=lead.payload or {},
    )


# ---------------------------------------------------------------------------
# Query builder — SQL text and parameter ordering
# ---------------------------------------------------------------------------


def test_query_always_carries_both_contact_branches():
    """The count must cover stamped-in-window AND in-flight (locked/
    PROCESSING) rows; dropping either branch silently reopens the
    cross-worker race or misses completed calls."""
    text, values = count_recent_contacted_leads_query(
        customer_mobile_number="+15551234567",
        reseller_id="res-1",
        window_start=WINDOW_START,
    )

    assert values == ["res-1", WINDOW_START, "+15551234567"]
    # Branch 1: dialled in window. IS NOT NULL is explicit so the partial
    # index from migration 054 matches.
    assert '"call_initiated_time" IS NOT NULL' in text
    assert '"call_initiated_time" >= $2' in text
    # Branch 2: in-flight, fresher than the window so crashed workers'
    # stale locks self-heal out of the count.
    assert '"is_locked" = TRUE' in text
    assert "'PROCESSING'" in text
    assert '"updated_at" >= $2' in text
    # Phone normalization on both sides.
    assert "regexp_replace($3" in text
    # None of the optional exclusions leaked into the base query.
    assert "request_id" not in text
    assert "merchant_id" not in text
    assert '"id" IS DISTINCT FROM' not in text


def test_query_optional_filters_append_in_order():
    """merchant_id, request_id and lead_id exclusions must land as $4/$5/$6
    in that exact order — the accessor passes them by position."""
    text, values = count_recent_contacted_leads_query(
        customer_mobile_number="9876543210",
        reseller_id="res-1",
        window_start=WINDOW_START,
        merchant_id="merchant-9",
        exclude_request_id="req-9",
        exclude_lead_id="lead-9",
    )

    assert values == [
        "res-1",
        WINDOW_START,
        "9876543210",
        "merchant-9",
        "req-9",
        "lead-9",
    ]
    assert '"merchant_id" = $4' in text
    assert '"request_id" IS DISTINCT FROM $5' in text
    assert '"id" IS DISTINCT FROM $6' in text
    # The in-flight branch still keys off the shared window parameter.
    assert text.count(">= $2") == 2


def test_query_each_exclusion_is_optional():
    """Passing only one exclusion must not shift the others' positions."""
    text, values = count_recent_contacted_leads_query(
        customer_mobile_number="9876543210",
        reseller_id="res-1",
        window_start=WINDOW_START,
        exclude_lead_id="lead-9",
    )

    assert values == ["res-1", WINDOW_START, "9876543210", "lead-9"]
    assert '"id" IS DISTINCT FROM $4' in text
    assert "request_id" not in text
    assert "merchant_id" not in text


# ---------------------------------------------------------------------------
# recent_contact_cooldown — decision and accessor boundary
# ---------------------------------------------------------------------------


@pytest.fixture
def count_spy(monkeypatch):
    """Capture the kwargs the function passes to the accessor; the accessor's
    return value is set per-test."""
    captured: Dict[str, Any] = {}
    state = {"count": 0}

    async def _count(**kwargs: Any) -> Optional[int]:
        captured.update(kwargs)
        return state["count"]

    monkeypatch.setattr(fc, "count_recent_contacted_leads", _count)
    return captured, state


async def test_no_recent_contact_proceeds(count_spy):
    captured, state = count_spy
    state["count"] = 0

    ctx = make_ctx(lead_id="lead-A", request_id="req-A")
    assert await fc.recent_contact_cooldown(ctx) is True


async def test_recent_contact_blocks(count_spy):
    _, state = count_spy
    state["count"] = 1

    assert await fc.recent_contact_cooldown(make_ctx()) is False


async def test_self_and_retry_family_are_excluded(count_spy):
    """The lead is locked while its pre-checks run, and its retry siblings
    share its request_id — both would poison the count if not excluded."""
    captured, _ = count_spy

    ctx = make_ctx(lead_id="lead-A", request_id="req-A")
    await fc.recent_contact_cooldown(ctx)

    assert captured["exclude_lead_id"] == "lead-A"
    assert captured["exclude_request_id"] == "req-A"
    assert captured["reseller_id"] == "res-1"
    assert captured["customer_mobile_number"] == "+15551234567"


async def test_merchant_scopes(count_spy):
    """'*' resolves to no merchant filter; an explicit merchant overrides the
    lead's own; no arg falls back to the lead's own merchant."""
    captured, _ = count_spy

    await fc.recent_contact_cooldown(make_ctx(args={"merchant_id": "*"}))
    assert captured["merchant_id"] is None

    await fc.recent_contact_cooldown(make_ctx(args={"merchant_id": "merchant-x"}))
    assert captured["merchant_id"] == "merchant-x"

    await fc.recent_contact_cooldown(make_ctx(merchant_id="merchant-1"))
    assert captured["merchant_id"] == "merchant-1"


async def test_window_hours_arg(count_spy):
    """Config values arrive as strings through placeholder resolution; a
    non-positive value must fall back to the 24h default."""
    captured, _ = count_spy
    ctx = make_ctx(args={"window_hours": "2"})

    await fc.recent_contact_cooldown(ctx)
    span = datetime.now(timezone.utc) - captured["window_start"]
    assert span.total_seconds() == pytest.approx(2 * 3600, abs=5)

    await fc.recent_contact_cooldown(make_ctx(args={"window_hours": -1}))
    span = datetime.now(timezone.utc) - captured["window_start"]
    assert span.total_seconds() == pytest.approx(24 * 3600, abs=5)


async def test_lookup_failure_raises(count_spy):
    """None from the accessor means "we don't know" — the function must
    raise so default_on_failure decides, never guess no-contact."""
    _, state = count_spy
    state["count"] = None

    with pytest.raises(RuntimeError):
        await fc.recent_contact_cooldown(make_ctx())


async def test_missing_phone_raises(count_spy):
    """A lead with no number in its payload is a data bug — surface it to
    default_on_failure instead of silently dialling through."""
    ctx = make_ctx(phone=None)
    with pytest.raises(ValueError):
        await fc.recent_contact_cooldown(ctx)
