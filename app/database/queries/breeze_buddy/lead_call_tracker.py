"""
Database query functions for the application.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.schemas import CallDirection, ExecutionMode, LeadCallStatus

# Table names
LEAD_CALL_TRACKER_TABLE = "lead_call_tracker"
OUTBOUND_NUMBER_TABLE = "outbound_number"


# Lead call tracker queries
def insert_lead_call_tracker_query(
    id: str,
    reseller_id: str,
    template: str,
    merchant_id: Optional[str],
    next_attempt_at: Optional[datetime],
    payload: Optional[Dict[str, Any]],
    meta_data: Optional[Dict[str, Any]],
    call_end_time: Optional[datetime] = None,
    attempt_count: int = 0,
    call_initiated_time: Optional[datetime] = None,
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
) -> Tuple[str, List[Any]]:
    """
    Generate query to insert lead call tracker record.

    Args:
        reseller_id: Reseller ID (also used as merchant_id for backward compatibility)
        template_id: UUID of the template (preferred, for referential integrity)
        template: Name of the template (kept for backward compatibility)
        execution_mode: Execution mode (TELEPHONY, TELEPHONY_TEST, DAILY, DAILY_TEST)
        call_id: Call SID (optional, used for inbound calls where call_sid is known upfront)
        outbound_number_id: Outbound number ID (optional, used for inbound calls)
        call_direction: Direction of call (INBOUND or OUTBOUND)
        outcome: Call outcome (optional, used for blocked calls e.g. BLOCKED_REJECT, BLOCKED_REDIRECT)
    """
    text = f"""
        INSERT INTO "{LEAD_CALL_TRACKER_TABLE}"
        (
            "id",
            "reseller_id",
            "template",
            "template_id",
            "merchant_id",
            "request_id",
            "next_attempt_at",
            "payload",
            "meta_data",
            "status",
            "call_initiated_time",
            "call_end_time",
            "attempt_count",
            "cost",
            "execution_mode",
            "call_id",
            "outbound_number_id",
            "call_direction",
            "outcome",
            "created_at",
            "updated_at"
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21) RETURNING *;
    """

    values = [
        id,
        reseller_id,
        template,
        template_id,
        merchant_id,
        request_id,
        next_attempt_at,
        json.dumps(payload) if payload else None,
        json.dumps(meta_data) if meta_data else None,
        status.value,  # Use parameter (defaults to BACKLOG for backward compatibility)
        call_initiated_time,
        call_end_time,
        attempt_count,
        cost,
        execution_mode.value,
        call_id,
        outbound_number_id,
        call_direction.value,
        outcome,
        datetime.now(),
        datetime.now(),
    ]

    return text, values


def acquire_lock_on_lead_by_id_query(
    lead_id: str,
    expected_status: Optional[LeadCallStatus] = None,
    force: bool = False,
) -> Tuple[str, List[Any]]:
    """
    Generate query to atomically acquire lock on a lead by ID.

    If expected_status is provided, only acquires the lock when the lead's current
    status matches — combining lock + status verification in a single atomic query.
    Returns the lead if successfully locked, empty result if already locked or
    status mismatched.

    When the BACKLOG variant is used (dispatcher path), this also stamps
    ``dispatched_at = COALESCE(dispatched_at, NOW())`` — the first BACKLOG
    lock wins; subsequent locks (e.g. by ``reconcile_stuck_processing_leads``
    on a PROCESSING row) preserve the original timestamp. See
    docs/BACKLOG_DISPATCHER_REDESIGN.md.

    ``force=True`` skips the ``is_locked = FALSE`` guard, allowing the
    reconciler to reclaim a lock left dangling by a crashed pod. Only safe
    because the BackgroundTaskScheduler's distributed lock ensures a single
    reconciler runs at a time and only calls this after a 10-minute timeout.
    """
    set_clause = '"is_locked" = TRUE, "updated_at" = NOW()'
    if expected_status == LeadCallStatus.BACKLOG:
        set_clause += ', "dispatched_at" = COALESCE("dispatched_at", NOW())'

    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET {set_clause}
        WHERE "id" = $1
    """
    if not force:
        text += '        AND "is_locked" = FALSE\n'
    values: List[Any] = [lead_id]
    if expected_status is not None:
        text += f'        AND "status" = ${len(values) + 1}\n'
        values.append(expected_status.value)
    text += "        RETURNING *;\n    "
    return text, values


def release_lock_on_lead_by_id_query(lead_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to release lock on a lead by ID.
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET "is_locked" = FALSE, "updated_at" = NOW()
        WHERE "id" = $1
        RETURNING *;
    """
    values = [lead_id]
    return text, values


def defer_lead_next_attempt_and_release_lock_query(
    lead_id: str, defer_seconds: int
) -> Tuple[str, List[Any]]:
    """
    Generate query to release the lock on a lead and push next_attempt_at
    forward by at least defer_seconds. Used when a dispatch worker cannot
    place the call right now (rate-limited, outside calling hours, channel
    saturated, etc.) so the next promoter tick doesn't immediately re-pick
    the same lead.

    GREATEST() preserves any further-in-the-future schedule already on the
    row, and COALESCE() guards against NULL next_attempt_at so the deferral
    always takes effect.
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET "is_locked" = FALSE,
            "next_attempt_at" = GREATEST(
                COALESCE("next_attempt_at", NOW()),
                NOW() + make_interval(secs => $2::int)
            ),
            "updated_at" = NOW()
        WHERE "id" = $1
        RETURNING *;
    """
    values: List[Any] = [lead_id, defer_seconds]
    return text, values


def update_lead_call_details_query(
    id: str,
    status: LeadCallStatus,
    call_id: str,
    call_initiated_time: datetime,
    outbound_number_id: str,
) -> Tuple[str, List[Any]]:
    """
    Generate query to update lead call details.
    Only updates if lead is still in BACKLOG status to prevent concurrent
    process_backlog_leads invocations from overwriting an active call's details.
    Returns zero rows if the lead was already moved to PROCESSING/FINISHED by another invocation.
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET "status" = $1, "call_id" = $2, "updated_at" = NOW(), "call_initiated_time" = $3, "outbound_number_id" = $4
        WHERE "id" = $5 AND "status" = $6
        RETURNING *;
    """
    values = [
        status.value,
        call_id,
        call_initiated_time,
        outbound_number_id,
        id,
        LeadCallStatus.BACKLOG.value,
    ]
    return text, values


def get_lead_by_call_id_query(call_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to get lead by call ID.
    """
    text = f"""
        SELECT *
        FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE "call_id" = $1;
    """
    values = [call_id]
    return text, values


def get_leads_by_request_id_query(request_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to get all leads by request_id.
    """
    text = f"""
        SELECT *
        FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE "request_id" = $1
        ORDER BY "created_at" DESC;
    """
    values = [request_id]
    return text, values


def get_lead_by_id_query(lead_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to get lead by ID.
    """
    text = f"""
        SELECT *
        FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE "id" = $1;
    """
    values = [lead_id]
    return text, values


def update_lead_call_recording_url_query(
    call_id: str, recording_url: str
) -> Tuple[str, List[Any]]:
    """
    Generate query to update lead call recording url.
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET "recording_url" = $1, "updated_at" = NOW()
        WHERE "call_id" = $2
        RETURNING *;
    """
    values = [recording_url, call_id]
    return text, values


def update_lead_call_initiated_time_query(
    call_id: str, call_initiated_time: datetime
) -> Tuple[str, List[Any]]:
    """
    Generate query to update lead call initiated time by call_id.
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET "call_initiated_time" = $1, "updated_at" = NOW()
        WHERE "call_id" = $2
        RETURNING *;
    """
    values = [call_initiated_time, call_id]
    return text, values


def update_lead_call_initiated_time_by_id_query(
    lead_id: str, call_initiated_time: datetime
) -> Tuple[str, List[Any]]:
    """
    Generate query to update lead call initiated time by lead id.
    Used for Daily mode where there's no telephony call_id.
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET "call_initiated_time" = $1, "updated_at" = NOW()
        WHERE "id" = $2
        RETURNING *;
    """
    values = [call_initiated_time, lead_id]
    return text, values


def update_lead_call_id_by_id_query(
    lead_id: str, call_id: str
) -> Tuple[str, List[Any]]:
    """
    Generate query to update lead call_id by lead id.
    Used for Daily mode to store the Daily room name as call_id
    so that recording webhooks can look up the lead by room name.
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET "call_id" = $1, "updated_at" = NOW()
        WHERE "id" = $2
        RETURNING *;
    """
    values = [call_id, lead_id]
    return text, values


def update_lead_call_completion_details_query(
    id: str,
    status: Optional[LeadCallStatus] = None,
    outcome: Optional[str] = None,
    meta_data: Optional[Dict[str, Any]] = None,
    call_end_time: Optional[datetime] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to update lead call completion details.
    Only updates fields that are not None.
    """
    set_clauses: List[str] = []
    values: List[Any] = []

    if status is not None:
        values.append(status.value)
        set_clauses.append(f'"status" = ${len(values)}')

    if outcome is not None:
        values.append(outcome)
        set_clauses.append(f'"outcome" = ${len(values)}')

    if meta_data is not None:
        values.append(json.dumps(meta_data))
        set_clauses.append(f'"meta_data" = ${len(values)}')

    if call_end_time is not None:
        values.append(call_end_time)
        set_clauses.append(f'"call_end_time" = ${len(values)}')

    # Always update updated_at
    set_clauses.append('"updated_at" = NOW()')

    # Add id for WHERE clause
    values.append(id)
    where_clause = f'"id" = ${len(values)}'

    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET {", ".join(set_clauses)}
        WHERE {where_clause}
        RETURNING *;
    """

    return text, values


def get_all_lead_call_trackers_query(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    outcome: Optional[str] = None,
    request_id: Optional[str] = None,
    shop_name: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to get all lead call trackers within a date range with optional filters and pagination.
    """
    text = f"""
        SELECT
            lct.*,
            ou.provider as calling_provider
        FROM
            "{LEAD_CALL_TRACKER_TABLE}" lct
        LEFT JOIN
            "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id
    """
    values: List[Any] = []
    conditions = []

    if start_date:
        values.append(start_date)
        conditions.append(f'lct."call_initiated_time" >= ${len(values)}')

    if end_date:
        values.append(end_date)
        conditions.append(f'lct."call_initiated_time" < ${len(values)}')

    if outcome:
        values.append(outcome)
        conditions.append(f"outcome = ${len(values)}")

    if request_id:
        values.append(f"%{request_id}%")
        conditions.append(f"lct.request_id LIKE ${len(values)}")

    if shop_name:
        values.append(f"%{shop_name}%")
        conditions.append(f"payload->>'shop_name' LIKE ${len(values)}")

    if conditions:
        text += " WHERE " + " AND ".join(conditions)

    text += ' ORDER BY lct."created_at" DESC'

    if limit is not None:
        values.append(limit)
        text += f" LIMIT ${len(values)}"

    if offset is not None:
        values.append(offset)
        text += f" OFFSET ${len(values)}"

    text += ";"
    return text, values


def get_leads_by_status_and_time_before_query(
    status: LeadCallStatus, time: datetime, include_locked: bool = False
) -> Tuple[str, List[Any]]:
    """
    Generate query to select leads based on their status and a time before which
    they were initiated. By default excludes locked rows (safe for BACKLOG/RETRY).
    Pass include_locked=True for the PROCESSING reconciler path where leads are
    always locked by design.
    """
    text = f"""
        SELECT * FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE "status" = $1
        AND "call_initiated_time" < $2
    """
    if not include_locked:
        text += '        AND "is_locked" = FALSE\n'
    text += ";"
    values = [status.value, time]
    return text, values


def get_lead_call_trackers_count_query(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    outcome: Optional[str] = None,
    request_id: Optional[str] = None,
    shop_name: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to count all lead call trackers within a date range with optional filters.
    """
    text = f"""
        SELECT COUNT(*) FROM "{LEAD_CALL_TRACKER_TABLE}"
    """
    values: List[Any] = []
    conditions = []

    if start_date:
        values.append(start_date)
        conditions.append(f'"{LEAD_CALL_TRACKER_TABLE}"."created_at" >= ${len(values)}')

    if end_date:
        values.append(end_date)
        conditions.append(f'"{LEAD_CALL_TRACKER_TABLE}"."created_at" < ${len(values)}')

    if outcome:
        values.append(outcome)
        conditions.append(f"outcome = ${len(values)}")

    if request_id:
        values.append(f"%{request_id}%")
        conditions.append(f"request_id LIKE ${len(values)}")

    if shop_name:
        values.append(f"%{shop_name}%")
        conditions.append(f"payload->>'shop_name' LIKE ${len(values)}")

    if conditions:
        text += " WHERE " + " AND ".join(conditions)

    text += ";"
    return text, values


def get_lead_based_analytics_query(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to get per-lead call data.
    Returns one row per request_id with call counts. Aggregation done in Python.
    """
    values: List[Any] = []
    conditions = []

    if start_date:
        values.append(start_date)
        conditions.append(f'"call_initiated_time" >= ${len(values)}')

    if end_date:
        values.append(end_date)
        conditions.append(f'"call_initiated_time" < ${len(values)}')

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    text = f"""
        SELECT
            request_id AS order_id,
            COUNT(*) AS total_calls,
            COUNT(*) FILTER (WHERE status = 'FINISHED') AS finished_calls,
            COUNT(*) FILTER (WHERE outcome = 'CONFIRM') AS confirmed_calls,
            COUNT(*) FILTER (WHERE outcome = 'CANCEL') AS cancelled_calls,
            COUNT(*) FILTER (WHERE outcome = 'ABORT') AS aborted_calls,
            COUNT(*) FILTER (WHERE outcome = 'ADDRESS_UPDATED') AS address_update_calls,
            COUNT(*) FILTER (WHERE outcome = 'BUSY') AS busy_calls,
            COUNT(*) FILTER (WHERE outcome = 'NO_ANSWER') AS no_answer_calls
        FROM "{LEAD_CALL_TRACKER_TABLE}"
        {where_clause}
        GROUP BY request_id;
    """
    return text, values


def update_langfuse_scores_query(
    call_id: str, langfuse_scores: Dict[str, Any]
) -> Tuple[str, List[Any]]:
    """
    Update the langfuse_scores column for a specific call_id.

    Args:
        call_id: The call identifier (call_sid)
        langfuse_scores: JSON object containing Langfuse evaluation scores

    Returns:
        Tuple of (query_text, values)
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET langfuse_scores = $1::jsonb, updated_at = NOW()
        WHERE call_id = $2
        RETURNING *;
    """
    values = [json.dumps(langfuse_scores), call_id]
    return text, values


def abort_lead_by_id_query(
    lead_id: str, cancellation_reason: str
) -> Tuple[str, List[Any]]:
    """
    Generate query to abort a lead by lead ID.
    Sets status to FINISHED and outcome to ABORT.

    Args:
        lead_id: Lead UUID
        cancellation_reason: Optional reason for cancellation
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET 
            "status" = $1, 
            "outcome" = $2, 
            "updated_at" = NOW(),
            "meta_data" = COALESCE("meta_data", '{{}}')::jsonb || $3::jsonb
        WHERE 
            "id" = $4
            AND "status" IN ($5, $6)
            AND ("outcome" IS NULL OR "outcome" = '')
        RETURNING *;
    """

    metadata = {
        "aborted_at": datetime.now().isoformat(),
        "outcome": {"abort_reason": cancellation_reason},
    }

    values = [
        LeadCallStatus.FINISHED.value,
        "ABORT",  # Outcome string literal
        json.dumps(metadata),
        lead_id,
        LeadCallStatus.BACKLOG.value,
        LeadCallStatus.RETRY.value,
    ]
    return text, values


def update_lead_payload_query(
    lead_id: str, payload_updates: Dict[str, Any]
) -> Tuple[str, List[Any]]:
    """
    Update specific fields in the payload JSON column for a lead.

    Uses jsonb merge (||) to update only the specified fields without
    overwriting existing payload data.

    Args:
        lead_id: Lead UUID
        payload_updates: Dictionary of fields to merge into the existing payload

    Returns:
        Tuple of (query_text, values)
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET
            "payload" = COALESCE("payload", '{{}}')::jsonb || $1::jsonb,
            "updated_at" = NOW()
        WHERE "id" = $2
        RETURNING *;
    """
    values = [json.dumps(payload_updates), lead_id]
    return text, values


def update_lead_request_id_query(
    lead_id: str, request_id: str
) -> Tuple[str, List[Any]]:
    """
    Update the request_id column for a specific lead.

    Args:
        lead_id: Lead UUID
        request_id: The value to set as request_id (e.g. linked outbound lead UUID)

    Returns:
        Tuple of (query_text, values)
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET
            "request_id" = $1,
            "updated_at" = NOW()
        WHERE "id" = $2;
    """
    values = [request_id, lead_id]
    return text, values


def update_lead_template_query(
    lead_id: str, template: str, template_id: str
) -> Tuple[str, List[Any]]:
    """
    Update the template name and template_id for a lead.

    Used when an IVR selection results in a different template than the
    one the lead was initially created with in the answer handler.

    Args:
        lead_id: Lead UUID
        template: New template name
        template_id: New template UUID

    Returns:
        Tuple of (query_text, values)
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET
            "template" = $1,
            "template_id" = $2,
            "updated_at" = NOW()
        WHERE "id" = $3
        RETURNING *;
    """
    values = [template, template_id, lead_id]
    return text, values


def append_metadata_field_query(
    lead_id: str, field_updates: Dict[str, Any]
) -> Tuple[str, List[Any]]:
    """
    Append/merge specific fields into the meta_data JSONB column for a lead.

    Uses jsonb merge (||) to update only the specified fields without
    overwriting existing meta_data.

    Args:
        lead_id: Lead UUID
        field_updates: Dictionary of fields to merge into existing meta_data

    Returns:
        Tuple of (query_text, values)
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET
            "meta_data" = COALESCE("meta_data", '{{}}')::jsonb || $1::jsonb,
            "updated_at" = NOW()
        WHERE "id" = $2
        RETURNING *;
    """
    values = [json.dumps(field_updates), lead_id]
    return text, values


def reset_widget_voice_lead_query(
    lead_id: str,
    payload: Dict[str, Any],
    meta_data_seed: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """Reset a widget voice lead so the next /voice/connect can reuse it.

    Used only by the unified widget router (CHAT_MODE.md §14): one
    chat_session has ONE voice lead (set in chat_session.voice_lead_id)
    that persists for the conversation's lifetime; each /voice/connect
    after the first reuses this lead instead of creating a new row.

    Behaviour:
      - status flipped back to BACKLOG so the bot can run again
      - attempt_count incremented (analytics treats this attempt as
        the Nth voice attachment in the conversation)
      - payload REPLACED with the latest template_vars (chat may have
        accumulated new state since the prior voice attachment)
      - meta_data is *merged* with the new seed so widget-mode markers
        (is_widget, widget_config_id, widget_session_id) and the new
        seed (start_node, prior_history, seed_message_count) overwrite
        their prior values, but unrelated fields the bot wrote during
        the prior attempt (e.g., evaluator scores) are preserved
      - call_id, call_initiated_time, call_end_time, outcome, cost,
        recording_url are CLEARED so they don't leak from the prior
        attempt's call into this one
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET
            "status"               = $2,
            "attempt_count"        = COALESCE("attempt_count", 0) + 1,
            "payload"              = $3::jsonb,
            "meta_data"            = COALESCE("meta_data", '{{}}')::jsonb || $4::jsonb,
            "call_id"              = NULL,
            "call_initiated_time"  = NULL,
            "call_end_time"        = NULL,
            "outcome"              = NULL,
            "cost"                 = NULL,
            "recording_url"        = NULL,
            "updated_at"           = NOW()
        WHERE "id" = $1
        RETURNING *;
    """
    values: List[Any] = [
        lead_id,
        LeadCallStatus.BACKLOG.value,
        json.dumps(payload),
        json.dumps(meta_data_seed),
    ]
    return text, values
