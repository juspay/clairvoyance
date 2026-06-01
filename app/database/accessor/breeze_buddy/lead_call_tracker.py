"""
Database accessor functions for the application.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

from app.core.logger import logger
from app.database.decoder.breeze_buddy.lead_call_tracker import decode_lead_call_tracker
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.lead_call_tracker import (
    abort_lead_by_id_query,
    acquire_lock_on_lead_by_id_query,
    append_metadata_field_query,
    defer_lead_next_attempt_and_release_lock_query,
    get_all_lead_call_trackers_query,
    get_lead_based_analytics_query,
    get_lead_by_call_id_query,
    get_lead_by_id_query,
    get_lead_call_trackers_count_query,
    get_leads_by_request_id_query,
    get_leads_by_status_and_time_before_query,
    insert_lead_call_tracker_query,
    release_lock_on_lead_by_id_query,
    reset_widget_voice_lead_query,
    update_langfuse_scores_query,
    update_lead_call_completion_details_query,
    update_lead_call_details_query,
    update_lead_call_id_by_id_query,
    update_lead_call_initiated_time_by_id_query,
    update_lead_call_initiated_time_query,
    update_lead_call_recording_url_query,
    update_lead_payload_query,
    update_lead_request_id_query,
)
from app.schemas import (
    CallDirection,
    ExecutionMode,
    LeadCallStatus,
    LeadCallTracker,
)


def get_row_count(result: Optional[List[asyncpg.Record]]) -> int:
    """
    Get the number of rows in the result.
    """
    return len(result) if result else 0


async def create_lead_call_tracker(
    id: str,
    reseller_id: str,
    template: str,
    merchant_id: Optional[str],
    next_attempt_at: Optional[datetime],
    payload: Optional[Dict[str, Any]],
    attempt_count: int = 0,
    meta_data: Optional[Dict[str, Any]] = None,
    call_initiated_time: Optional[datetime] = None,
    call_end_time: Optional[datetime] = None,
    cost: Optional[float] = None,
    request_id: Optional[str] = None,
    template_id: Optional[str] = None,
    execution_mode: ExecutionMode = ExecutionMode.TELEPHONY,
    status: LeadCallStatus = LeadCallStatus.BACKLOG,  # Status with default for backward compatibility
    call_id: Optional[str] = None,  # For inbound calls where call_sid is known upfront
    outbound_number_id: Optional[str] = None,  # For inbound calls
    call_direction: CallDirection = CallDirection.OUTBOUND,  # Direction of call
    outcome: Optional[
        str
    ] = None,  # For blocked calls where outcome is known at insert time
) -> Optional[LeadCallTracker]:
    """
    Create a new lead call tracker record.

    Args:
        template_id: UUID of the template (preferred, for referential integrity)
        template: Name of the template (kept for backward compatibility)
        execution_mode: Execution mode (TELEPHONY, TELEPHONY_TEST, DAILY, DAILY_TEST)
        call_id: Call SID (optional, used for inbound calls where call_sid is known upfront)
        outbound_number_id: Outbound number ID (optional, used for inbound calls)
        call_direction: Direction of call (INBOUND or OUTBOUND, defaults to OUTBOUND)
        outcome: Call outcome (optional, e.g. BLOCKED_REJECT, BLOCKED_REDIRECT)
    """
    logger.info(f"Creating lead call tracker for reseller ID: {reseller_id}")

    try:
        query_text, values = insert_lead_call_tracker_query(
            id=id,
            reseller_id=reseller_id,
            template=template,
            template_id=template_id,
            merchant_id=merchant_id,
            next_attempt_at=next_attempt_at,
            payload=payload,
            meta_data=meta_data,
            call_initiated_time=call_initiated_time,
            call_end_time=call_end_time,
            attempt_count=attempt_count,
            cost=cost,
            request_id=request_id,
            execution_mode=execution_mode,
            status=status,  # Pass status (defaults to BACKLOG for backward compatibility)
            call_id=call_id,
            outbound_number_id=outbound_number_id,
            call_direction=call_direction,
            outcome=outcome,
        )

        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            logger.info(f"Lead call tracker created successfully: {decoded_result}")
            return decoded_result

        logger.error("Failed to create lead call tracker")
        return None

    except Exception as e:
        logger.error(f"Error creating lead call tracker: {e}")
        return None


async def acquire_lock_on_lead_by_id(
    lead_id: str,
    expected_status: Optional[LeadCallStatus] = None,
    force: bool = False,
) -> Optional[LeadCallTracker]:
    """
    Atomically acquire lock on a lead by ID.
    If expected_status is provided, only acquires when the lead's status matches —
    combining lock + status check in one DB round-trip instead of three (lock, check, release).
    Returns the locked lead if successful, None if already locked or status mismatched.

    ``force=True`` bypasses the ``is_locked = FALSE`` guard for the reconciler
    to reclaim dangling locks left by crashed pods.
    """
    logger.info(
        f"Attempting to acquire lock on lead with ID: {lead_id}"
        + (f" (expected_status={expected_status.value})" if expected_status else "")
        + (" [force]" if force else "")
    )

    try:
        query_text, values = acquire_lock_on_lead_by_id_query(
            lead_id, expected_status, force=force
        )
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            if decoded_result:
                logger.info(f"Lock acquired successfully for lead: {decoded_result.id}")
            else:
                logger.error("Lead decoding failed after acquiring lock")
            return decoded_result

        logger.info(f"Lead {lead_id} is already locked or does not exist")
        return None

    except Exception as e:
        logger.error(f"Error acquiring lock on lead: {e}")
        return None


async def release_lock_on_lead_by_id(lead_id: str) -> Optional[LeadCallTracker]:
    """
    Release lock on a lead by ID. Returns the unlocked lead if successful.
    """
    logger.info(f"Attempting to release lock on lead with ID: {lead_id}")

    try:
        query_text, values = release_lock_on_lead_by_id_query(lead_id)
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            if decoded_result:
                logger.info(f"Lock released successfully for lead: {decoded_result.id}")
            else:
                logger.error("Lead decoding failed after releasing lock")
            return decoded_result

        logger.warning(f"Lead {lead_id} not found for lock release")
        return None

    except Exception as e:
        logger.error(f"Error releasing lock on lead: {e}")
        return None


async def defer_lead_next_attempt_and_release_lock(
    lead_id: str, defer_seconds: int
) -> Optional[LeadCallTracker]:
    """
    Release the lock on a lead and push next_attempt_at out by at least
    defer_seconds. Used by the dispatcher worker when it can't place a
    call right now (rate-limited, outside calling hours, no channel
    available, etc.) — the DB write survives Redis loss; the matching
    ZADD onto the schedule is the fast path.
    """
    logger.info(
        f"Deferring next_attempt_at by {defer_seconds}s and releasing lock "
        f"for lead {lead_id}"
    )

    try:
        query_text, values = defer_lead_next_attempt_and_release_lock_query(
            lead_id, defer_seconds
        )
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            if decoded_result:
                logger.info(
                    f"Lead {decoded_result.id} deferred to "
                    f"{decoded_result.next_attempt_at} and unlocked"
                )
            else:
                logger.error("Lead decoding failed after defer + release")
            return decoded_result

        logger.warning(f"Lead {lead_id} not found for defer + release")
        return None

    except Exception as e:
        logger.error(f"Error deferring lead next_attempt_at: {e}")
        return None


async def update_lead_call_details(
    id: str,
    status: LeadCallStatus,
    call_id: str,
    call_initiated_time: datetime,
    outbound_number_id: str,
) -> Optional[LeadCallTracker]:
    """
    Update lead call details.
    Returns None (zero rows) if the lead was already moved out of BACKLOG
    by a concurrent invocation — this is an expected concurrency outcome.
    """
    logger.info(f"Updating lead {id} with status {status} and call ID {call_id}")

    try:
        query_text, values = update_lead_call_details_query(
            id, status, call_id, call_initiated_time, outbound_number_id
        )
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            logger.info(f"Lead updated successfully: {decoded_result}")
            return decoded_result

        logger.warning(f"Lead {id} not updated — status may have changed concurrently")
        return None

    except Exception as e:
        logger.error(f"Error updating lead: {e}")
        return None


async def get_lead_by_call_id(call_id: str) -> Optional[LeadCallTracker]:
    """
    Get lead by call ID.
    """
    logger.info(f"Getting lead with call ID {call_id}")

    try:
        query_text, values = get_lead_by_call_id_query(call_id)
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            logger.info(f"Lead found: {decoded_result}")
            return decoded_result

        logger.error("Lead not found")
        return None

    except Exception as e:
        logger.error(f"Error getting lead: {e}")
        return None


async def get_lead_by_id(lead_id: str) -> Optional[LeadCallTracker]:
    """
    Get lead by ID.
    """
    logger.info(f"Getting lead with ID {lead_id}")

    try:
        query_text, values = get_lead_by_id_query(lead_id)
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            logger.info(f"Lead found: {decoded_result}")
            return decoded_result

        logger.error("Lead not found")
        return None

    except Exception as e:
        logger.error(f"Error getting lead: {e}")
        return None


async def get_leads_by_request_id(
    request_id: str,
) -> List[LeadCallTracker]:
    """
    Get all leads by request_id.
    """
    logger.info(f"Getting leads with request_id {request_id}")

    query_text, values = get_leads_by_request_id_query(request_id)
    result = await run_parameterized_query(query_text, values)
    if result and get_row_count(result) > 0:
        decoded_results = [decode_lead_call_tracker(row) for row in result]
        decoded_results = [r for r in decoded_results if r is not None]
        logger.info(f"Found {len(decoded_results)} leads for request_id {request_id}")
        return decoded_results

    logger.info(f"No leads found for request_id {request_id}")
    return []


async def update_lead_call_initiated_time(
    call_id: str, call_initiated_time: datetime
) -> Optional[LeadCallTracker]:
    """
    Update lead call initiated time by call_id (telephony mode).
    """
    logger.info(f"Updating lead with call ID {call_id} with call initiated time")

    try:
        query_text, values = update_lead_call_initiated_time_query(
            call_id, call_initiated_time
        )
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            logger.info(f"Lead updated successfully: {decoded_result}")
            return decoded_result

        logger.error("Failed to update lead")
        return None

    except Exception as e:
        logger.error(f"Error updating lead: {e}")
        return None


async def update_lead_call_initiated_time_by_id(
    lead_id: str, call_initiated_time: datetime
) -> Optional[LeadCallTracker]:
    """
    Update lead call initiated time by lead id (Daily mode).
    Used when there's no telephony call_id.
    """
    logger.info(f"Updating lead {lead_id} with call initiated time")

    try:
        query_text, values = update_lead_call_initiated_time_by_id_query(
            lead_id, call_initiated_time
        )
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            logger.info(f"Lead updated successfully: {decoded_result}")
            return decoded_result

        logger.error("Failed to update lead")
        return None

    except Exception as e:
        logger.error(f"Error updating lead: {e}")
        return None


async def update_lead_call_id_by_id(
    lead_id: str, call_id: str
) -> Optional[LeadCallTracker]:
    """
    Update lead call_id by lead id (Daily mode).
    Used to store the Daily room name as call_id so that
    recording webhooks can look up the lead by room name.
    """
    logger.info(f"Updating lead {lead_id} with call_id: {call_id}")

    try:
        query_text, values = update_lead_call_id_by_id_query(lead_id, call_id)
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            logger.info(
                f"Lead call_id updated successfully for lead_id: {lead_id}, call_id: {call_id}"
            )
            return decoded_result

        logger.error(f"Failed to update lead call_id for lead_id: {lead_id}")
        return None

    except Exception as e:
        logger.error(f"Error updating lead call_id: {e}")
        return None


async def update_lead_call_recording_url(
    call_id: str, recording_url: str
) -> Optional[LeadCallTracker]:
    """
    Update lead call recording url.
    """
    logger.info(f"Updating lead with call ID {call_id} with recording url")

    try:
        query_text, values = update_lead_call_recording_url_query(
            call_id, recording_url
        )
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            logger.info(f"Lead updated successfully: {decoded_result}")
            return decoded_result

        logger.error("Failed to update lead")
        return None

    except Exception as e:
        logger.error(f"Error updating lead: {e}")
        return None


async def update_lead_call_completion_details(
    id: str,
    status: Optional[LeadCallStatus] = None,
    outcome: Optional[str] = None,
    meta_data: Optional[Dict[str, Any]] = None,
    call_end_time: Optional[datetime] = None,
) -> Optional[LeadCallTracker]:
    """
    Update lead call completion details.
    Only updates fields that are not None.
    """
    logger.info(f"Updating lead call completion details for ID: {id}")

    try:
        query_text, values = update_lead_call_completion_details_query(
            id, status, outcome, meta_data, call_end_time
        )
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            logger.info(
                f"Lead call completion details updated successfully: {decoded_result}"
            )
            return decoded_result

        logger.error("Failed to update lead call completion details")
        return None

    except Exception as e:
        logger.error(f"Error updating lead call completion details: {e}")
        return None


async def get_all_lead_call_trackers(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    outcome: Optional[str] = None,
    request_id: Optional[str] = None,
    shop_name: Optional[str] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
) -> List[Tuple[LeadCallTracker, Optional[str]]]:
    """
    Get all lead call trackers with optional filters and pagination.
    """
    logger.info("Getting all lead call trackers with filters and pagination")

    limit = page_size
    offset = (page - 1) * page_size if page and page_size else None

    try:
        query_text, values = get_all_lead_call_trackers_query(
            start_date=start_date,
            end_date=end_date,
            outcome=outcome,
            request_id=request_id,
            shop_name=shop_name,
            limit=limit,
            offset=offset,
        )
        result = await run_parameterized_query(query_text, values)
        if result:
            decoded_results = [
                (decode_lead_call_tracker(row), row["calling_provider"])
                for row in result
            ]
            return [
                (item, provider)
                for item, provider in decoded_results
                if item is not None
            ]
        return []
    except Exception as e:
        logger.error(f"Error getting all lead call trackers: {e}")
        return []


async def get_leads_by_status_and_time_before(
    status: LeadCallStatus, time: datetime, include_locked: bool = False
) -> List[LeadCallTracker]:
    """
    Get leads based on their status and a time before which they were initiated.
    Pass include_locked=True to include locked rows (used by the PROCESSING reconciler).
    """
    logger.info(f"Getting leads with status {status} initiated before {time}")

    try:
        query_text, values = get_leads_by_status_and_time_before_query(
            status, time, include_locked=include_locked
        )
        result = await run_parameterized_query(query_text, values)
        if result:
            decoded = [decode_lead_call_tracker(row) for row in result]
            return [item for item in decoded if item is not None]
        else:
            logger.info("No leads found matching criteria")
        return []
    except Exception as e:
        logger.error(f"Error getting leads: {e}")
        return []


async def get_lead_call_trackers_count(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    outcome: Optional[str] = None,
    request_id: Optional[str] = None,
    shop_name: Optional[str] = None,
) -> int:
    """
    Get the count of all lead call trackers with optional filters.
    """
    logger.info("Getting lead call trackers count with filters")

    try:
        query_text, values = get_lead_call_trackers_count_query(
            start_date=start_date,
            end_date=end_date,
            outcome=outcome,
            request_id=request_id,
            shop_name=shop_name,
        )
        result = await run_parameterized_query(query_text, values)
        if result and result[0]["count"]:
            return result[0]["count"]
        return 0
    except Exception as e:
        logger.error(f"Error getting lead call trackers count: {e}")
        return 0


async def get_lead_based_analytics(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> List[asyncpg.Record]:
    """
    Get per-lead call data for analytics.
    Returns list of records with call counts per order_id.
    """
    logger.info("Getting lead-based analytics data")

    try:
        query_text, values = get_lead_based_analytics_query(
            start_date=start_date,
            end_date=end_date,
        )
        result = await run_parameterized_query(query_text, values)
        return result if result else []
    except Exception as e:
        logger.error(f"Error getting lead-based analytics: {e}", exc_info=True)
        return []


async def update_langfuse_scores(
    call_id: str, langfuse_scores: Dict[str, Any]
) -> Optional[LeadCallTracker]:
    """
    Update the langfuse_scores column for a specific call_id.

    Args:
        call_id: The call identifier (call_sid)
        langfuse_scores: JSON object containing Langfuse evaluation scores

    Returns:
        Updated LeadCallTracker record if successful, None otherwise
    """
    logger.debug(f"Updating langfuse_scores for call_id: {call_id}")

    try:
        query_text, values = update_langfuse_scores_query(call_id, langfuse_scores)
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            return decoded_result
        return None
    except Exception as e:
        logger.error(f"Error updating langfuse_scores for call_id {call_id}: {e}")
        return None


async def handle_lead_abort(
    lead_id: str, cancellation_reason: str
) -> Optional[LeadCallTracker]:
    """
    Abort a lead by lead ID.
    Sets status to FINISHED and outcome to ABORT.
    """
    try:
        query_text, values = abort_lead_by_id_query(lead_id, cancellation_reason)
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            return decoded_result

        logger.error("Failed to abort lead")
        return None

    except Exception as e:
        logger.error(f"Error aborting lead {lead_id}: {e}")
        return None


async def update_lead_payload(
    lead_id: str, payload_updates: Dict[str, Any]
) -> Optional[LeadCallTracker]:
    """
    Update specific fields in the payload JSON column for a lead.
    Merges the provided updates into the existing payload without
    overwriting other fields.
    """
    logger.info(
        f"Updating lead payload for ID: {lead_id}, keys: {list(payload_updates.keys())}"
    )

    try:
        query_text, values = update_lead_payload_query(lead_id, payload_updates)
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            logger.info(f"Lead payload updated successfully for ID: {lead_id}")
            return decoded_result

        logger.error(f"Failed to update lead payload for ID: {lead_id}")
        return None

    except Exception as e:
        logger.error(f"Error updating lead payload for ID {lead_id}: {e}")
        return None


async def append_metadata_field(
    lead_id: str, field_updates: Dict[str, Any]
) -> Optional[LeadCallTracker]:
    """
    Append/merge specific fields into the meta_data JSONB column for a lead.
    Merges the provided updates into the existing meta_data without
    overwriting other fields.
    """
    logger.info(
        f"Appending metadata fields for lead ID: {lead_id}, keys: {list(field_updates.keys())}"
    )

    try:
        query_text, values = append_metadata_field_query(lead_id, field_updates)
        result = await run_parameterized_query(query_text, values)

        if result and get_row_count(result) > 0:
            decoded_result = decode_lead_call_tracker(result[0])
            logger.info(f"Lead metadata updated successfully for ID: {lead_id}")
            return decoded_result

        logger.error(f"Failed to update lead metadata for ID: {lead_id}")
        return None

    except Exception as e:
        logger.error(f"Error updating lead metadata for ID {lead_id}: {e}")
        return None


async def update_lead_request_id(lead_id: str, request_id: str) -> None:
    """
    Update the request_id column for a lead.

    Used to link an inbound lead to its associated outbound (hold-transfer)
    lead after the outbound leg is created.

    Args:
        lead_id: UUID of the lead to update (inbound lead)
        request_id: Value to set as request_id (outbound lead UUID)
    """
    logger.info(f"Updating request_id for lead ID: {lead_id} → {request_id}")
    try:
        query_text, values = update_lead_request_id_query(lead_id, request_id)
        await run_parameterized_query(query_text, values)
        logger.info(f"request_id updated successfully for lead ID: {lead_id}")
    except Exception as e:
        logger.error(f"Error updating request_id for lead ID {lead_id}: {e}")


async def reset_widget_voice_lead(
    lead_id: str,
    payload: Dict[str, Any],
    meta_data_seed: Dict[str, Any],
) -> Optional[LeadCallTracker]:
    """Reset a widget voice lead so the next /voice/connect can reuse it.

    Used only by the unified widget router (CHAT_MODE.md §14): one
    chat_session has ONE voice lead (set in chat_session.voice_lead_id)
    that persists for the conversation's lifetime. Each /voice/connect
    after the first reuses this lead — bumps attempt_count, refreshes
    the seed (start_node, prior_history), clears per-call fields
    (call_id, outcome, etc.) so they don't leak from the previous
    attempt.
    """
    logger.info(
        f"Resetting widget voice lead {lead_id} for next attempt "
        f"(seed: {sorted(meta_data_seed.keys())})"
    )
    query_text, values = reset_widget_voice_lead_query(lead_id, payload, meta_data_seed)
    try:
        result = await run_parameterized_query(query_text, values)
        if result and get_row_count(result) > 0:
            return decode_lead_call_tracker(result[0])
        logger.error(f"Failed to reset widget voice lead {lead_id} (no row updated)")
        return None
    except Exception as e:
        logger.error(f"Error resetting widget voice lead {lead_id}: {e}")
        raise
