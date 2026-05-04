"""
Pickup Rate Calculator

Computes call-based and lead-based pickup rates for a given time window,
mirroring the logic in ScoreMonitor._get_daily_call_stats().
"""

from datetime import datetime
from typing import Any, Dict, Optional

from app.core.logger import logger
from app.database.accessor.breeze_buddy.lead_call_tracker import (
    get_call_based_pickup_rate,
    get_lead_based_analytics,
)

# Sentinel value returned on DB error - callers must check for None
_ERROR_RESULT = None


async def compute_pickup_rates(
    start_date: datetime,
    end_date: datetime,
    merchant_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Calculate call-based and lead-based pickup rates for the given window.

    Args:
        start_date:   Start of the rolling window (UTC-aware).
        end_date:     End of the rolling window (UTC-aware).
        merchant_id:  Restrict results to this merchant (None = all merchants).

    Returns:
        Dictionary with the following keys on success::

            {
                "calls_attempted":    int,   # FINISHED calls in window
                "calls_picked":       int,   # attempted - NO_ANSWER
                "calls_no_answer":    int,
                "call_pickup_rate":   float, # (picked / attempted) * 100
                "total_leads":        int,   # unique request_ids
                "leads_picked":       int,   # leads where finished > no_answer
                "lead_pickup_rate":   float, # (leads_picked / total_leads) * 100
            }

        Returns ``None`` if a DB error occurs (caller should skip alerting).
    """
    try:
        # ------------------------------------------------------------------
        # 1. Call-based metrics - single SQL aggregation (no Python-side loop)
        # ------------------------------------------------------------------
        calls_attempted, calls_no_answer = await get_call_based_pickup_rate(
            start_date=start_date,
            end_date=end_date,
            merchant_id=merchant_id,
        )

        calls_picked = calls_attempted - calls_no_answer
        call_pickup_rate = (
            calls_picked / calls_attempted * 100 if calls_attempted > 0 else 0.0
        )

        # ------------------------------------------------------------------
        # 2. Lead-based metrics
        # ------------------------------------------------------------------
        lead_data = await get_lead_based_analytics(
            start_date=start_date,
            end_date=end_date,
            merchant_id=merchant_id,
        )

        total_leads = len(lead_data) if lead_data else 0
        leads_picked = 0

        if lead_data:
            for lead in lead_data:
                # A lead is "picked" when more calls were finished than went unanswered
                if lead["finished_calls"] > lead["no_answer_calls"]:
                    leads_picked += 1

        lead_pickup_rate = leads_picked / total_leads * 100 if total_leads > 0 else 0.0

        result = {
            "calls_attempted": calls_attempted,
            "calls_picked": calls_picked,
            "calls_no_answer": calls_no_answer,
            "call_pickup_rate": call_pickup_rate,
            "total_leads": total_leads,
            "leads_picked": leads_picked,
            "lead_pickup_rate": lead_pickup_rate,
        }

        logger.debug(
            f"compute_pickup_rates [{start_date} -> {end_date}] "
            f"call_rate={call_pickup_rate}% lead_rate={lead_pickup_rate}%"
        )
        return result

    except Exception as e:
        logger.error(
            f"compute_pickup_rates failed for window [{start_date} -> {end_date}]: {e}",
            exc_info=True,
        )
        return _ERROR_RESULT
