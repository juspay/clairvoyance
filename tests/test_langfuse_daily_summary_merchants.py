"""Tests for the "Calls by Merchant" block of the Langfuse daily Slack summary.

Outcomes are free-form per template, so the block picks each merchant's top
outcomes from the data; only NO_ANSWER / BUSY (set by platform code) are
treated specially. The flipkart counts are the real last-24h numbers from
2026-09-26 ~01:00 IST; the Shopify split below NO_ANSWER is illustrative.

The DB accessor is patched, so nothing here touches Postgres.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from app.services.langfuse.tasks.score_monitor import score as score_module
from app.services.langfuse.tasks.score_monitor.score import (
    ScoreMonitor,
    format_merchant_breakdown,
)

ALL_CALLS = 143242


def _rows(merchant_id, outcomes):
    total = sum(outcomes.values())
    return [
        {
            "merchant_id": merchant_id,
            "outcome": outcome,
            "calls": calls,
            "merchant_calls": total,
            "all_calls": ALL_CALLS,
        }
        for outcome, calls in sorted(outcomes.items(), key=lambda i: -i[1])
    ]


FLIPKART = {
    "NO_ANSWER": 71280,
    "BUSY": 34648,
    "NOT_INTERESTED": 4471,
    "VOICEMAIL": 4282,
    "USER_BUSY": 4113,
    "ISSUE_REPORTED": 3675,
    "USER_WILL_COMPLETE_LATER": 2398,
    "UNSUPPORTED_LANGUAGE": 1083,
    "OTHER_OUTCOMES": 1249,
}
SHOPIFY_STORE = {
    "NO_ANSWER": 3770,
    "BUSY": 300,
    "CONFIRM": 250,
    "CANCEL": 90,
    "UNKNOWN": 50,
    "ADDRESS_UPDATED": 15,
}


def test_breakdown_formats_top_merchants_and_outcomes():
    rows = _rows("flipkart", FLIPKART) + _rows(
        "vnhnaiduhall.myshopify.com", SHOPIFY_STORE
    )

    text = format_merchant_breakdown(rows)

    assert text == (
        "• *flipkart* — 127,199 calls · answered 44.0% · no result 62.0% of answered\n"
        "   ↳ NOT_INTERESTED 8.0% · VOICEMAIL 7.7% · USER_BUSY 7.4%\n"
        "• *vnhnaiduhall.myshopify.com* — 4,475 calls · answered 15.8% · "
        "no result 42.6% of answered\n"
        "   ↳ CONFIRM 35.5% · CANCEL 12.8% · UNKNOWN 7.1%\n"
        "• Other merchants — 11,568 calls"
    )


def test_breakdown_handles_null_merchant_and_no_named_outcomes():
    rows = [
        {
            "merchant_id": None,
            "outcome": outcome,
            "calls": calls,
            "merchant_calls": 10,
            "all_calls": 10,
        }
        for outcome, calls in [("NO_ANSWER", 6), ("BUSY", 3), (None, 1)]
    ]

    text = format_merchant_breakdown(rows)

    # no named outcomes -> no "↳" line; no remainder -> no "Other" line
    assert text == (
        "• *(no merchant)* — 10 calls · answered 40.0% · no result 75.0% of answered"
    )


def test_breakdown_empty_returns_none():
    assert format_merchant_breakdown([]) is None


async def test_merchant_breakdown_failure_is_isolated(monkeypatch):
    monkeypatch.setattr(
        score_module,
        "get_daily_summary_merchant_outcomes",
        AsyncMock(side_effect=RuntimeError("db down")),
    )

    monitor = ScoreMonitor.__new__(ScoreMonitor)

    assert await monitor._get_merchant_breakdown() is None
