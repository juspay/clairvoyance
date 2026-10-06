"""
Accessor functions for the event-driven dispatcher.
"""

from datetime import datetime
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

from app.core.logger import logger
from app.database.decoder.breeze_buddy.lead_call_tracker import decode_lead_call_tracker
from app.database.decoder.breeze_buddy.telephony_number import (
    decode_telephony_number_list,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.dispatch import (
    clean_stale_bb_locks_query,
    count_processing_by_telephony_number_query,
    get_due_backlog_page_query,
    get_finished_inbound_calls_query,
    get_known_inbound_calls_query,
    get_lead_dispatch_states_query,
    get_legacy_inflight_leads_query,
    get_live_calls_on_number_query,
    get_live_calls_on_numbers_query,
    get_telephony_numbers_by_ids_query,
    get_unscheduled_backlog_leads_query,
    set_telephony_number_channels_query,
    update_lead_next_attempt_at_query,
)
from app.database.queries.breeze_buddy.telephony_number import (
    get_all_telephony_numbers_query,
)
from app.schemas import LeadCallTracker, TelephonyNumber


async def get_unscheduled_backlog_leads(
    lookahead_seconds: int = 120, limit: int = 1000
) -> List[Tuple[str, str, int, Optional[str]]]:
    """
    Return ``(id, reseller_id, score_ms, template_id)`` tuples for BACKLOG leads due
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
        return [
            (r["id"], r["reseller_id"], int(r["score_ms"]), r["template_id"])
            for r in rows
        ]
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


# ---------------------------------------------------------------------------
# v2 event dialler. Every accessor below raises on a DB error, so a caller can
# tell "nothing there" from "could not read" and take its safe default.
# ---------------------------------------------------------------------------


class LeadDispatchState(NamedTuple):
    status: str
    is_locked: bool
    template_id: Optional[str]
    next_attempt_at: Optional[datetime]


async def get_due_backlog_page(
    after: Optional[Tuple[datetime, str]], limit: int, lookahead_seconds: int = 120
) -> List[Tuple[str, Optional[str], datetime]]:
    """``(id, template_id, next_attempt_at)`` of the next keyset page after ``after``."""
    try:
        query, values = get_due_backlog_page_query(after, limit, lookahead_seconds)
        rows = await run_parameterized_query(query, values)
        return [(r["id"], r["template_id"], r["next_attempt_at"]) for r in rows or []]
    except Exception as e:
        logger.error(f"get_due_backlog_page failed: {e}", exc_info=True)
        raise


async def get_lead_dispatch_states(
    lead_ids: List[str],
) -> Dict[str, LeadDispatchState]:
    """``{lead_id: state}``; ids with no row are absent."""
    if not lead_ids:
        return {}
    try:
        query, values = get_lead_dispatch_states_query(lead_ids)
        rows = await run_parameterized_query(query, values)
        return {
            r["id"]: LeadDispatchState(
                r["status"],
                bool(r["is_locked"]),
                r["template_id"],
                r["next_attempt_at"],
            )
            for r in rows or []
        }
    except Exception as e:
        logger.error(f"get_lead_dispatch_states failed: {e}", exc_info=True)
        raise


async def get_live_calls_on_number(
    number_id: str,
) -> List[Tuple[str, str, Optional[str]]]:
    """``(lead_id, call_direction, call_id)`` of every call holding a line on the number."""
    try:
        query, values = get_live_calls_on_number_query(number_id)
        rows = await run_parameterized_query(query, values)
        return [(r["id"], r["call_direction"], r["call_id"]) for r in rows or []]
    except Exception as e:
        logger.error(f"get_live_calls_on_number failed: {e}", exc_info=True)
        raise


async def get_live_calls_on_numbers(
    number_ids: List[str],
) -> Dict[str, List[Tuple[str, str, Optional[str]]]]:
    """``{number_id: [(lead_id, call_direction, call_id)]}`` of every call holding a line
    on these numbers; a number with none is absent."""
    if not number_ids:
        return {}
    try:
        query, values = get_live_calls_on_numbers_query(number_ids)
        rows = await run_parameterized_query(query, values)
        out: Dict[str, List[Tuple[str, str, Optional[str]]]] = {}
        for r in rows or []:
            out.setdefault(str(r["telephony_number_id"]), []).append(
                (r["id"], r["call_direction"], r["call_id"])
            )
        return out
    except Exception as e:
        logger.error(f"get_live_calls_on_numbers failed: {e}", exc_info=True)
        raise


async def get_finished_inbound_calls(call_ids: List[str]) -> Set[str]:
    """The inbound call ids among ``call_ids`` whose leads are all FINISHED."""
    if not call_ids:
        return set()
    try:
        query, values = get_finished_inbound_calls_query(call_ids)
        rows = await run_parameterized_query(query, values)
        return {r["call_id"] for r in rows or []}
    except Exception as e:
        logger.error(f"get_finished_inbound_calls failed: {e}", exc_info=True)
        raise


async def get_known_inbound_calls(call_ids: List[str]) -> Set[str]:
    """The inbound call ids among ``call_ids`` that have a lead row."""
    if not call_ids:
        return set()
    try:
        query, values = get_known_inbound_calls_query(call_ids)
        rows = await run_parameterized_query(query, values)
        return {r["call_id"] for r in rows or []}
    except Exception as e:
        logger.error(f"get_known_inbound_calls failed: {e}", exc_info=True)
        raise


async def get_legacy_inflight_leads(
    lead_ids: List[str],
) -> List[Tuple[str, Optional[str], bool]]:
    """``(lead_id, template_id, is_locked)`` of locked BACKLOG leads and of ``lead_ids``
    still in BACKLOG."""
    try:
        query, values = get_legacy_inflight_leads_query(lead_ids)
        rows = await run_parameterized_query(query, values)
        return [(r["id"], r["template_id"], bool(r["is_locked"])) for r in rows or []]
    except Exception as e:
        logger.error(f"get_legacy_inflight_leads failed: {e}", exc_info=True)
        raise


async def get_telephony_numbers_by_ids(
    number_ids: List[str],
) -> Dict[str, TelephonyNumber]:
    """``{number_id: number}``; ids with no row are absent."""
    if not number_ids:
        return {}
    try:
        query, values = get_telephony_numbers_by_ids_query(number_ids)
        rows = await run_parameterized_query(query, values)
        return {n.id: n for n in decode_telephony_number_list(rows)}
    except Exception as e:
        logger.error(f"get_telephony_numbers_by_ids failed: {e}", exc_info=True)
        raise


async def list_telephony_numbers() -> List[TelephonyNumber]:
    """Every telephony number. Raises on a DB error, unlike
    ``get_all_telephony_numbers`` (which returns []): the v2 recovery after a Redis loss
    must not read "unreadable" as "no numbers"."""
    try:
        query, values = get_all_telephony_numbers_query()
        rows = await run_parameterized_query(query, values)
        return decode_telephony_number_list(rows)
    except Exception as e:
        logger.error(f"list_telephony_numbers failed: {e}", exc_info=True)
        raise


async def set_telephony_number_channels(number_id: str, channels: int) -> bool:
    """Set ``channels`` outright. False if the number has no row."""
    try:
        query, values = set_telephony_number_channels_query(number_id, channels)
        return bool(await run_parameterized_query(query, values))
    except Exception as e:
        logger.error(f"set_telephony_number_channels failed: {e}", exc_info=True)
        raise
