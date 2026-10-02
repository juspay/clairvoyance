"""
Accessor functions for the event-driven dispatcher.
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple

from app.core.logger import logger
from app.database.decoder.breeze_buddy.lead_call_tracker import decode_lead_call_tracker
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.dispatch import (
    clean_stale_bb_locks_query,
    count_processing_by_telephony_number_query,
    get_unscheduled_backlog_leads_query,
    park_lead_until_and_release_lock_query,
    update_lead_next_attempt_at_query,
    wake_window_parked_leads_query,
)
from app.schemas import LeadCallTracker


async def get_unscheduled_backlog_leads(
    lookahead_seconds: int = 120, limit: int = 1000
) -> List[Tuple[str, str, int]]:
    """
    Return ``(id, reseller_id, score_ms)`` triples for BACKLOG leads due
    within the lookahead window. Used by ``reconcile_backlog_to_zset`` to
    detect and re-emit lost ZADD events.
    """
    try:
        query, values = get_unscheduled_backlog_leads_query(
            lookahead_seconds=lookahead_seconds, limit=limit
        )
        rows = await run_parameterized_query(query, values)
        if not rows:
            return []
        return [(r["id"], r["reseller_id"], int(r["score_ms"])) for r in rows]
    except Exception as e:
        logger.error(f"get_unscheduled_backlog_leads failed: {e}", exc_info=True)
        raise


async def count_processing_by_telephony_number() -> Dict[str, int]:
    """Return ``{telephony_number_id: in_flight_count}`` for active calls."""
    try:
        query, values = count_processing_by_telephony_number_query()
        rows = await run_parameterized_query(query, values)
        if not rows:
            return {}
        return {r["telephony_number_id"]: int(r["in_flight"]) for r in rows}
    except Exception as e:
        logger.error(f"count_processing_by_telephony_number failed: {e}", exc_info=True)
        raise


async def clean_stale_bb_locks(threshold_minutes: int = 10) -> List[str]:
    """Unlock rows stuck with ``is_locked=TRUE``. Returns the freed ids."""
    try:
        query, values = clean_stale_bb_locks_query(threshold_minutes=threshold_minutes)
        rows = await run_parameterized_query(query, values)
        return [r["id"] for r in (rows or [])]
    except Exception as e:
        logger.error(f"clean_stale_bb_locks failed: {e}", exc_info=True)
        raise


async def update_lead_next_attempt_at_now(
    lead_id: str, next_attempt_at: datetime
) -> Optional[LeadCallTracker]:
    """Bump ``next_attempt_at`` on a BACKLOG row. Returns the updated row."""
    try:
        query, values = update_lead_next_attempt_at_query(
            lead_id=lead_id, next_attempt_at=next_attempt_at
        )
        rows = await run_parameterized_query(query, values)
        if not rows:
            return None
        return decode_lead_call_tracker(rows[0])
    except Exception as e:
        logger.error(f"update_lead_next_attempt_at_now failed: {e}", exc_info=True)
        raise


async def park_lead_until_and_release_lock(
    lead_id: str, park_until: datetime
) -> Optional[LeadCallTracker]:
    """Park an out-of-hours lead until ``park_until`` and unlock it.
    Returns the updated row, or None if the lead is gone."""
    try:
        query, values = park_lead_until_and_release_lock_query(lead_id, park_until)
        rows = await run_parameterized_query(query, values)
        if not rows:
            return None
        return decode_lead_call_tracker(rows[0])
    except Exception as e:
        logger.error(f"park_lead_until_and_release_lock failed: {e}", exc_info=True)
        raise


async def wake_window_parked_leads(
    template_id: str,
    parked_at: List[datetime],
    wake_at: datetime,
) -> List[Tuple[str, datetime]]:
    """Move leads parked at an old window opening to ``wake_at``. Returns
    ``(id, next_attempt_at)`` for every moved row."""
    try:
        query, values = wake_window_parked_leads_query(
            template_id=template_id, parked_at=parked_at, wake_at=wake_at
        )
        rows = await run_parameterized_query(query, values)
        return [(r["id"], r["next_attempt_at"]) for r in (rows or [])]
    except Exception as e:
        logger.error(f"wake_window_parked_leads failed: {e}", exc_info=True)
        raise
