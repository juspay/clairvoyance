"""Tests for the Langfuse daily Slack summary call stats.

The summary used to load every lead_call_tracker row of the last 24h into
memory (~150k rows) and count in Python, which OOM-killed the pod. The counts
now come back as one aggregated row; these tests pin the derived stats to the
exact dict the old code logged in production on 2026-09-21 23:41 IST, so the
Slack message stays identical.

The DB accessor is patched, so nothing here touches Postgres.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.langfuse.tasks.score_monitor import score as score_module
from app.services.langfuse.tasks.score_monitor.score import ScoreMonitor

# Raw counts for the 21 Sep window, as returned by get_daily_summary_stats
COUNTS_21_SEP = {
    "calls_attempted": 32300,
    "calls_no_answer": 16722,
    "calls_confirm": 632,
    "calls_cancel": 46,
    "calls_address_updated": 15,
    "calls_busy": 5847,
    "provider_twilio": 0,
    "provider_exotel": 0,
    "provider_plivo": 32239,
    "total_leads": 11709,
    "leads_picked": 6876,
    "leads_confirmed": 632,
    "leads_cancelled": 46,
    "leads_address_updated": 15,
}

# "Daily call stats: {...}" logged by the pre-fix code on 21 Sep
LOGGED_STATS_21_SEP = {
    "calls_attempted": 32300,
    "calls_picked": 15578,
    "calls_picked_pct": 48.2,
    "calls_successful": 693,
    "calls_successful_pct": 4.4,
    "calls_busy": 5847,
    "calls_busy_pct": 37.5,
    "total_leads": 11709,
    "leads_picked": 6876,
    "leads_picked_pct": 58.7,
    "leads_successful": 693,
    "leads_successful_pct": 10.1,
    "leads_confirmed": 632,
    "leads_confirmed_pct": 91.2,
    "leads_cancelled": 46,
    "leads_cancelled_pct": 6.6,
    "leads_address_updated": 15,
    "leads_address_updated_pct": 2.2,
    "provider_split": {"TWILIO": 0, "EXOTEL": 0, "PLIVO": 32239},
}


def _monitor() -> ScoreMonitor:
    # Skip __init__: it wires the Langfuse client, which these tests don't need
    return ScoreMonitor.__new__(ScoreMonitor)


async def test_daily_call_stats_match_pre_fix_output(monkeypatch):
    stats_mock = AsyncMock(return_value=COUNTS_21_SEP)
    monkeypatch.setattr(score_module, "get_daily_summary_stats", stats_mock)

    stats = await _monitor()._get_daily_call_stats()

    assert stats == LOGGED_STATS_21_SEP
    stats_mock.assert_awaited_once()
    assert stats_mock.await_args is not None
    window = stats_mock.await_args.kwargs
    assert (window["end_date"] - window["start_date"]).total_seconds() == 24 * 3600


@pytest.mark.parametrize("db_result", [None, {}])
async def test_daily_call_stats_falls_back_to_zeros(monkeypatch, db_result):
    monkeypatch.setattr(
        score_module, "get_daily_summary_stats", AsyncMock(return_value=db_result)
    )

    stats = await _monitor()._get_daily_call_stats()

    assert stats["calls_attempted"] == 0
    assert stats["total_leads"] == 0
    assert stats["provider_split"] == {"TWILIO": 0, "EXOTEL": 0, "PLIVO": 0}
