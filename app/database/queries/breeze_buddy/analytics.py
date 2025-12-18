"""
Generic analytics query builder for template-agnostic analytics.
All filtering is done at database level for optimal performance.
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Tuple


# Table names
LEAD_CALL_TRACKER_TABLE = "lead_call_tracker"
OUTBOUND_NUMBER_TABLE = "outbound_number"

# Pattern for valid JSONB key names (alphanumeric, underscore, hyphen only)
# This prevents SQL injection via malicious key names in payload filters
VALID_JSONB_KEY_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+$')


def is_valid_payload_filter_key(key: str) -> bool:
    """
    Validate that a payload filter key is safe to use in SQL queries.

    Only allows alphanumeric characters, underscores, and hyphens.
    Rejects any keys containing SQL metacharacters like quotes, semicolons, etc.

    Args:
        key: The JSONB key name to validate

    Returns:
        True if key is safe, False otherwise
    """
    if not key or len(key) > 100:  # Reject empty or excessively long keys
        return False
    return bool(VALID_JSONB_KEY_PATTERN.match(key))


def build_analytics_where_clause(
    filters: Dict[str, Any],
    value_offset: int = 0
) -> Tuple[List[str], List[Any]]:
    """
    Build WHERE clause conditions and values from generic filters.

    Args:
        filters: Dictionary of filter key-value pairs
        value_offset: Starting index for parameterized query values

    Returns:
        Tuple of (conditions list, values list)
    """
    conditions = []
    values = []

    # Date range filters
    if "date_from" in filters and filters["date_from"]:
        date_from = filters["date_from"]
        if isinstance(date_from, datetime):
            values.append(date_from)
        else:
            values.append(datetime.combine(date_from, datetime.min.time()))
        conditions.append(f'lct."call_initiated_time" >= ${len(values) + value_offset}')

    if "date_to" in filters and filters["date_to"]:
        date_to = filters["date_to"]
        if isinstance(date_to, datetime):
            values.append(date_to)
        else:
            values.append(datetime.combine(date_to, datetime.max.time()))
        conditions.append(f'lct."call_initiated_time" < ${len(values) + value_offset}')

    # Standard column filters
    if "template" in filters and filters["template"]:
        values.append(filters["template"])
        conditions.append(f'lct."template" = ${len(values) + value_offset}')

    if "merchant_id" in filters and filters["merchant_id"]:
        values.append(filters["merchant_id"])
        conditions.append(f'lct."merchant_id" = ${len(values) + value_offset}')

    if "merchant_ids" in filters and filters["merchant_ids"]:
        # Use ANY for array matching
        values.append(filters["merchant_ids"])
        conditions.append(f'lct."merchant_id" = ANY(${len(values) + value_offset})')

    if "shop_identifier" in filters and filters["shop_identifier"]:
        values.append(filters["shop_identifier"])
        conditions.append(f'lct."shop_identifier" = ${len(values) + value_offset}')

    if "shop_identifiers" in filters and filters["shop_identifiers"]:
        values.append(filters["shop_identifiers"])
        conditions.append(f'lct."shop_identifier" = ANY(${len(values) + value_offset})')

    if "status" in filters and filters["status"]:
        values.append(filters["status"])
        conditions.append(f'lct."status" = ${len(values) + value_offset}')

    if "outcome" in filters and filters["outcome"]:
        values.append(filters["outcome"])
        conditions.append(f'lct."outcome" = ${len(values) + value_offset}')

    if "request_id" in filters and filters["request_id"]:
        values.append(filters["request_id"])
        conditions.append(f'lct."request_id" = ${len(values) + value_offset}')

    # Generic payload filters (JSONB queries)
    # Validate keys to prevent SQL injection
    if "payload_filters" in filters and filters["payload_filters"]:
        for key, value in filters["payload_filters"].items():
            # Skip invalid keys silently to prevent SQL injection
            if not is_valid_payload_filter_key(key):
                continue
            values.append(value)
            conditions.append(f"lct.payload->>'{key}' = ${len(values) + value_offset}")

    return conditions, values


def get_analytics_summary_query(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """
    Generate query for summary analytics with aggregations done at DB level.
    Returns counts, averages, and outcome breakdowns.
    """
    conditions, values = build_analytics_where_clause(filters)
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    text = f"""
        SELECT
            COUNT(*) as total_calls,
            COUNT(*) FILTER (WHERE lct.status = 'FINISHED') as completed_calls,
            COUNT(*) FILTER (WHERE lct.status != 'FINISHED' OR lct.status IS NULL) as failed_calls,
            AVG(
                EXTRACT(EPOCH FROM (lct.call_end_time - lct.call_initiated_time))
            ) FILTER (
                WHERE lct.call_initiated_time IS NOT NULL
                AND lct.call_end_time IS NOT NULL
            ) as average_duration,
            COUNT(DISTINCT lct.template) as total_templates,
            COUNT(DISTINCT lct.shop_identifier) FILTER (WHERE lct.shop_identifier IS NOT NULL) as total_shops,
            jsonb_object_agg(
                COALESCE(lct.outcome, 'N/A'),
                outcome_counts.count
            ) as outcome_breakdown
        FROM "{LEAD_CALL_TRACKER_TABLE}" lct
        LEFT JOIN "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id
        LEFT JOIN LATERAL (
            SELECT lct2.outcome, COUNT(*) as count
            FROM "{LEAD_CALL_TRACKER_TABLE}" lct2
            {where_clause.replace('lct.', 'lct2.')}
            GROUP BY lct2.outcome
        ) outcome_counts ON true
        {where_clause};
    """

    return text, values


def get_analytics_call_details_query(
    filters: Dict[str, Any],
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "created_at",
    sort_order: str = "desc"
) -> Tuple[str, List[Any]]:
    """
    Generate query for paginated call details.
    """
    conditions, values = build_analytics_where_clause(filters)
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    # Validate sort column to prevent SQL injection
    allowed_sort_columns = ["created_at", "call_initiated_time", "call_end_time", "updated_at"]
    if sort_by not in allowed_sort_columns:
        sort_by = "created_at"

    sort_direction = "DESC" if sort_order.lower() == "desc" else "ASC"

    text = f"""
        SELECT
            lct.*,
            ou.provider as calling_provider
        FROM "{LEAD_CALL_TRACKER_TABLE}" lct
        LEFT JOIN "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id
        {where_clause}
        ORDER BY lct."{sort_by}" {sort_direction}
        LIMIT ${len(values) + 1}
        OFFSET ${len(values) + 2};
    """

    values.extend([limit, offset])
    return text, values


def get_analytics_count_query(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """
    Generate query to count records matching filters.
    """
    conditions, values = build_analytics_where_clause(filters)
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    text = f"""
        SELECT COUNT(*) as count
        FROM "{LEAD_CALL_TRACKER_TABLE}" lct
        {where_clause};
    """

    return text, values


def get_analytics_trends_query(
    filters: Dict[str, Any],
    time_granularity: str = "day"
) -> Tuple[str, List[Any]]:
    """
    Generate query for trends analytics with time bucketing done at DB level.
    """
    conditions, values = build_analytics_where_clause(filters)
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    # Determine date truncation based on granularity
    if time_granularity == "week":
        date_trunc = "week"
    elif time_granularity == "month":
        date_trunc = "month"
    else:
        date_trunc = "day"

    text = f"""
        SELECT
            DATE_TRUNC('{date_trunc}', lct.call_initiated_time) as time_bucket,
            COUNT(*) as total_calls,
            COUNT(*) FILTER (WHERE lct.status = 'FINISHED') as completed_calls,
            AVG(
                EXTRACT(EPOCH FROM (lct.call_end_time - lct.call_initiated_time))
            ) FILTER (
                WHERE lct.call_initiated_time IS NOT NULL
                AND lct.call_end_time IS NOT NULL
            ) as average_duration,
            jsonb_object_agg(
                COALESCE(lct.outcome, 'N/A'),
                outcome_counts.count
            ) as outcome_breakdown
        FROM "{LEAD_CALL_TRACKER_TABLE}" lct
        LEFT JOIN LATERAL (
            SELECT lct2.outcome, COUNT(*) as count
            FROM "{LEAD_CALL_TRACKER_TABLE}" lct2
            WHERE DATE_TRUNC('{date_trunc}', lct2.call_initiated_time) = DATE_TRUNC('{date_trunc}', lct.call_initiated_time)
            {(' AND ' + ' AND '.join([c.replace('lct.', 'lct2.') for c in conditions])) if conditions else ''}
            GROUP BY lct2.outcome
        ) outcome_counts ON true
        {where_clause}
        AND lct.call_initiated_time IS NOT NULL
        GROUP BY time_bucket
        ORDER BY time_bucket ASC;
    """

    return text, values


def get_analytics_lead_based_query(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """
    Generate query for lead-based analytics (one row per unique lead/request_id).
    Generic - no hardcoded outcomes.
    """
    conditions, values = build_analytics_where_clause(filters)
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    text = f"""
        SELECT
            lct.request_id,
            COUNT(*) as total_calls,
            COUNT(*) FILTER (WHERE lct.status = 'FINISHED') as finished_calls,
            COUNT(*) FILTER (WHERE lct.outcome IS NOT NULL) as calls_with_outcome,
            jsonb_object_agg(
                COALESCE(outcome_counts.outcome, 'N/A'),
                outcome_counts.count
            ) as outcome_breakdown
        FROM "{LEAD_CALL_TRACKER_TABLE}" lct
        LEFT JOIN LATERAL (
            SELECT lct2.outcome, COUNT(*) as count
            FROM "{LEAD_CALL_TRACKER_TABLE}" lct2
            WHERE lct2.request_id = lct.request_id
            {(' AND ' + ' AND '.join([c.replace('lct.', 'lct2.') for c in conditions])) if conditions else ''}
            GROUP BY lct2.outcome
        ) outcome_counts ON true
        {where_clause}
        GROUP BY lct.request_id
        HAVING lct.request_id IS NOT NULL;
    """

    return text, values


def get_analytics_outbound_numbers_query(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """
    Generate query for outbound numbers analytics.
    """
    conditions, values = build_analytics_where_clause(filters)
    # Always include the NOT NULL condition to ensure valid SQL
    conditions.append("ou.number IS NOT NULL")
    where_clause = " WHERE " + " AND ".join(conditions)

    text = f"""
        SELECT
            ou.number,
            ou.provider,
            COUNT(*) as total_calls,
            COUNT(*) FILTER (WHERE lct.outcome != 'NO_ANSWER' AND lct.outcome IS NOT NULL) as calls_picked,
            COUNT(*) FILTER (WHERE lct.outcome = 'NO_ANSWER') as calls_no_answer
        FROM "{LEAD_CALL_TRACKER_TABLE}" lct
        LEFT JOIN "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id
        {where_clause}
        GROUP BY ou.number, ou.provider
        ORDER BY total_calls DESC;
    """

    return text, values
