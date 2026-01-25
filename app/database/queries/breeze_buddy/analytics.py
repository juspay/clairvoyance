"""
Generic analytics query builder for template-agnostic analytics.
All filtering is done at database level for optimal performance.
"""

import re
import uuid as uuid_module
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.core.logger import logger

# Table names
LEAD_CALL_TRACKER_TABLE = "lead_call_tracker"
OUTBOUND_NUMBER_TABLE = "outbound_number"

# Pattern for valid JSONB key names (alphanumeric, underscore, hyphen only)
# This prevents SQL injection via malicious key names in payload filters
VALID_JSONB_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def is_uuid(value: str) -> bool:
    """
    Check if a string is a valid UUID.

    Args:
        value: String to check

    Returns:
        True if value is a valid UUID, False otherwise
    """
    try:
        uuid_module.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


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
    value_offset: int = 0,
    filter_execution_mode: bool = True,
) -> Tuple[List[str], List[Any]]:
    """
    Build WHERE clause conditions and values from generic filters.

    Args:
        filters: Dictionary of filter key-value pairs
        value_offset: Starting index for parameterized query values
        filter_execution_mode: If True, only include TELEPHONY execution_mode (exclude tests)

    Returns:
        Tuple of (conditions list, values list)
    """
    conditions = []
    values = []

    # Filter by execution_mode to exclude test calls from analytics
    if filter_execution_mode:
        conditions.append("lct.execution_mode = 'TELEPHONY'")

    # Date range filters
    if "date_from" in filters and filters["date_from"]:
        date_from = filters["date_from"]
        if isinstance(date_from, datetime):
            values.append(date_from)
        else:
            values.append(datetime.combine(date_from, datetime.min.time()))
        conditions.append(f"lct.call_initiated_time >= ${len(values) + value_offset}")

    if "date_to" in filters and filters["date_to"]:
        date_to = filters["date_to"]
        if isinstance(date_to, datetime):
            values.append(date_to)
        else:
            values.append(datetime.combine(date_to, datetime.max.time()))
        conditions.append(f"lct.call_initiated_time < ${len(values) + value_offset}")

    # Standard column filters
    # Template filter - supports BOTH template name and template_id for backward compatibility
    if "template" in filters and filters["template"]:
        template_value = filters["template"]
        # Auto-detect if it's a UUID (template_id) or a name (template)
        if is_uuid(template_value):
            # Filter by template_id (UUID)
            values.append(template_value)
            conditions.append(f"lct.template_id = ${len(values) + value_offset}::UUID")
            logger.debug(f"Filtering by template_id (UUID): {template_value}")
        else:
            # Filter by template name (backward compat)
            values.append(template_value)
            conditions.append(f"lct.template = ${len(values) + value_offset}")
            logger.debug(f"Filtering by template name: {template_value}")

    # Explicit template_id filter (takes precedence if both provided)
    if "template_id" in filters and filters["template_id"]:
        values.append(filters["template_id"])
        conditions.append(f"lct.template_id = ${len(values) + value_offset}::UUID")

    if "merchant_id" in filters and filters["merchant_id"]:
        values.append(filters["merchant_id"])
        conditions.append(f"lct.merchant_id = ${len(values) + value_offset}")

    if "merchant_ids" in filters and filters["merchant_ids"]:
        # Use ANY for array matching
        values.append(filters["merchant_ids"])
        conditions.append(f"lct.merchant_id = ANY(${len(values) + value_offset})")

    if "shop_identifier" in filters and filters["shop_identifier"]:
        values.append(filters["shop_identifier"])
        conditions.append(f"lct.shop_identifier = ${len(values) + value_offset}")

    if "shop_identifiers" in filters and filters["shop_identifiers"]:
        values.append(filters["shop_identifiers"])
        conditions.append(f"lct.shop_identifier = ANY(${len(values) + value_offset})")

    if "status" in filters and filters["status"]:
        values.append(filters["status"])
        conditions.append(f"lct.status = ${len(values) + value_offset}")

    if "outcome" in filters and filters["outcome"]:
        values.append(filters["outcome"])
        conditions.append(f"lct.outcome = ANY(${len(values) + value_offset})")

    if "request_id" in filters and filters["request_id"]:
        values.append(filters["request_id"])
        conditions.append(f"lct.request_id = ${len(values) + value_offset}")

    # Provider filter (list of strings)
    if "provider" in filters and filters["provider"]:
        values.append(filters["provider"])
        conditions.append(f"ou.provider = ANY(${len(values) + value_offset})")

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


def get_analytics_summary_query(
    filters: Dict[str, Any], group_by: Optional[str] = None
) -> Tuple[str, List[Any]]:
    """
    Generate query for summary analytics with aggregations done at DB level.
    Returns counts, averages, and outcome breakdowns.

    Args:
        filters: Analytics filters
        group_by: Optional field to group by (e.g., 'shop_identifier', 'template')

    Uses a filtered_data CTE to avoid duplicating WHERE clauses and parameters.
    """
    conditions, values = build_analytics_where_clause(filters)
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    # Add LEFT JOIN only if provider filter is present
    join_clause = (
        f'LEFT JOIN "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id'
        if "provider" in filters and filters["provider"]
        else ""
    )

    # Validate group_by to prevent SQL injection
    allowed_group_by_fields = ["shop_identifier", "template", "merchant_id"]
    if group_by and group_by not in allowed_group_by_fields:
        group_by = None

    if group_by:
        # Grouped analytics
        text = f"""
            WITH filtered_data AS (
                SELECT
                    lct.status,
                    lct.outcome,
                    lct.call_initiated_time,
                    lct.call_end_time,
                    lct.template,
                    lct.shop_identifier,
                    lct.merchant_id,
                    lct.payload
                FROM "{LEAD_CALL_TRACKER_TABLE}" lct
                {join_clause}
                {where_clause}
            ),
            outcome_groups AS (
                SELECT
                    {group_by},
                    outcome,
                    COUNT(*) as outcome_count
                FROM filtered_data
                GROUP BY {group_by}, outcome
            )
            SELECT
                fd.{group_by},
                (SELECT payload->>'shop_name' FROM filtered_data WHERE {group_by} = fd.{group_by} LIMIT 1) as shop_name,
                COUNT(*) as total_calls,
                COUNT(*) FILTER (WHERE fd.status = 'FINISHED') as completed_calls,
                COUNT(*) FILTER (WHERE fd.status != 'FINISHED' OR fd.status IS NULL) as failed_calls,
                AVG(
                    EXTRACT(EPOCH FROM (fd.call_end_time - fd.call_initiated_time))
                ) FILTER (
                    WHERE fd.call_initiated_time IS NOT NULL
                    AND fd.call_end_time IS NOT NULL
                ) as average_duration,
                COUNT(DISTINCT fd.template) as total_templates,
                COUNT(DISTINCT fd.shop_identifier) FILTER (WHERE fd.shop_identifier IS NOT NULL) as total_shops,
                (
                    SELECT jsonb_object_agg(COALESCE(outcome, 'N/A'), outcome_count)
                    FROM outcome_groups og
                    WHERE og.{group_by} = fd.{group_by}
                ) as outcome_breakdown
            FROM filtered_data fd
            GROUP BY fd.{group_by}
            ORDER BY total_calls DESC;
        """
    else:
        # Aggregate analytics (original behavior)
        text = f"""
            WITH filtered_data AS (
                SELECT
                    lct.status,
                    lct.outcome,
                    lct.call_initiated_time,
                    lct.call_end_time,
                    lct.template,
                    lct.shop_identifier
                FROM "{LEAD_CALL_TRACKER_TABLE}" lct
                {join_clause}
                {where_clause}
            ),
            base_stats AS (
                SELECT
                    COUNT(*) as total_calls,
                    COUNT(*) FILTER (WHERE status = 'FINISHED') as completed_calls,
                    COUNT(*) FILTER (WHERE status != 'FINISHED' OR status IS NULL) as failed_calls,
                    AVG(
                        EXTRACT(EPOCH FROM (call_end_time - call_initiated_time))
                    ) FILTER (
                        WHERE call_initiated_time IS NOT NULL
                        AND call_end_time IS NOT NULL
                    ) as average_duration,
                    COUNT(DISTINCT template) as total_templates,
                    COUNT(DISTINCT shop_identifier) FILTER (WHERE shop_identifier IS NOT NULL) as total_shops
                FROM filtered_data
            ),
            outcome_stats AS (
                SELECT
                    jsonb_object_agg(
                        COALESCE(outcome, 'N/A'),
                        outcome_count
                    ) as outcome_breakdown
                FROM (
                    SELECT
                        outcome,
                        COUNT(*) as outcome_count
                    FROM filtered_data
                    GROUP BY outcome
                ) grouped_outcomes
            )
            SELECT
                base_stats.*,
                COALESCE(outcome_stats.outcome_breakdown, '{{}}'::jsonb) as outcome_breakdown
            FROM base_stats
            CROSS JOIN outcome_stats;
        """

    return text, values


def get_analytics_call_details_query(
    filters: Dict[str, Any],
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> Tuple[str, List[Any]]:
    """
    Generate query for paginated call details.
    Shows ALL execution modes (no filter) - call records include test calls.
    """
    # filter_execution_mode=False to show ALL modes including test calls
    conditions, values = build_analytics_where_clause(
        filters, filter_execution_mode=False
    )
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    # Validate sort column to prevent SQL injection
    allowed_sort_columns = [
        "created_at",
        "call_initiated_time",
        "call_end_time",
        "updated_at",
    ]
    if sort_by not in allowed_sort_columns:
        logger.warning(
            f"[Analytics Query] Invalid sort column '{sort_by}', defaulting to 'created_at'"
        )
        sort_by = "created_at"

    sort_direction = "DESC" if sort_order.lower() == "desc" else "ASC"

    text = f"""
        SELECT
            lct.*,
            ou.provider as calling_provider
        FROM "{LEAD_CALL_TRACKER_TABLE}" lct
        LEFT JOIN "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id
        {where_clause}
        ORDER BY lct.{sort_by} {sort_direction}
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

    # Add LEFT JOIN only if provider filter is present
    join_clause = (
        f'LEFT JOIN "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id'
        if "provider" in filters and filters["provider"]
        else ""
    )

    text = f"""
        SELECT COUNT(*) as count
        FROM "{LEAD_CALL_TRACKER_TABLE}" lct
        {join_clause}
        {where_clause};
    """

    return text, values


def get_analytics_trends_query(
    filters: Dict[str, Any], time_granularity: str = "day"
) -> Tuple[str, List[Any]]:
    """
    Generate query for trends analytics with time bucketing done at DB level.

    Uses a filtered_data CTE to avoid duplicating WHERE clauses and parameters.
    """
    conditions, values = build_analytics_where_clause(filters)
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    # Add LEFT JOIN only if provider filter is present
    join_clause = (
        f'LEFT JOIN "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id'
        if "provider" in filters and filters["provider"]
        else ""
    )

    # Determine date truncation based on granularity
    if time_granularity == "week":
        date_trunc = "week"
    elif time_granularity == "month":
        date_trunc = "month"
    else:
        date_trunc = "day"

    # Add call_initiated_time NOT NULL condition
    extra_condition = "lct.call_initiated_time IS NOT NULL"
    if where_clause:
        where_clause = f"{where_clause} AND {extra_condition}"
    else:
        where_clause = f" WHERE {extra_condition}"

    text = f"""
        WITH filtered_data AS (
            SELECT
                lct.call_initiated_time,
                lct.call_end_time,
                lct.status,
                lct.outcome
            FROM "{LEAD_CALL_TRACKER_TABLE}" lct
            {join_clause}
            {where_clause}
        ),
        base_trends AS (
            SELECT
                DATE_TRUNC('{date_trunc}', call_initiated_time) as time_bucket,
                COUNT(*) as total_calls,
                COUNT(*) FILTER (WHERE status = 'FINISHED') as completed_calls,
                AVG(
                    EXTRACT(EPOCH FROM (call_end_time - call_initiated_time))
                ) FILTER (
                    WHERE call_initiated_time IS NOT NULL
                    AND call_end_time IS NOT NULL
                ) as average_duration
            FROM filtered_data
            GROUP BY time_bucket
        ),
        outcome_trends AS (
            SELECT
                time_bucket,
                jsonb_object_agg(
                    COALESCE(outcome, 'N/A'),
                    outcome_count
                ) as outcome_breakdown
            FROM (
                SELECT
                    DATE_TRUNC('{date_trunc}', filtered_data.call_initiated_time) as time_bucket,
                    filtered_data.outcome,
                    COUNT(*) as outcome_count
                FROM filtered_data
                GROUP BY 1, 2
            ) grouped_outcomes
            GROUP BY time_bucket
        )
        SELECT
            base_trends.time_bucket,
            base_trends.total_calls,
            base_trends.completed_calls,
            base_trends.average_duration,
            COALESCE(outcome_trends.outcome_breakdown, '{{}}'::jsonb) as outcome_breakdown
        FROM base_trends
        LEFT JOIN outcome_trends ON base_trends.time_bucket = outcome_trends.time_bucket
        ORDER BY time_bucket ASC;
    """

    return text, values


def get_analytics_lead_based_query(
    filters: Dict[str, Any], group_by: Optional[str] = None
) -> Tuple[str, List[Any]]:
    """
    Generate query for lead-based analytics (one row per unique lead/request_id).
    Generic - no hardcoded outcomes.

    Args:
        filters: Analytics filters
        group_by: Optional field to group by (e.g., 'shop_identifier', 'template')

    Uses a filtered_data CTE to avoid duplicating WHERE clauses and parameters.
    """
    conditions, values = build_analytics_where_clause(filters)
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    # Add LEFT JOIN only if provider filter is present
    join_clause = (
        f'LEFT JOIN "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id'
        if "provider" in filters and filters["provider"]
        else ""
    )

    # Add request_id NOT NULL condition
    extra_condition = "lct.request_id IS NOT NULL"
    if where_clause:
        where_clause = f"{where_clause} AND {extra_condition}"
    else:
        where_clause = f" WHERE {extra_condition}"

    # Validate group_by to prevent SQL injection
    allowed_group_by_fields = ["shop_identifier", "template", "merchant_id"]
    if group_by and group_by not in allowed_group_by_fields:
        group_by = None

    if group_by:
        # Grouped lead-based analytics
        text = f"""
            WITH filtered_data AS (
                SELECT
                    lct.request_id,
                    lct.{group_by},
                    lct.status,
                    lct.outcome,
                    lct.payload
                FROM "{LEAD_CALL_TRACKER_TABLE}" lct
                {join_clause}
                {where_clause}
            ),
            unique_leads AS (
                SELECT DISTINCT
                    request_id,
                    {group_by}
                FROM filtered_data
            ),
            outcome_counts AS (
                SELECT
                    {group_by},
                    outcome,
                    COUNT(DISTINCT request_id) as outcome_count
                FROM filtered_data
                WHERE outcome IS NOT NULL
                GROUP BY {group_by}, outcome
            )
            SELECT
                ul.{group_by},
                (SELECT payload->>'shop_name' FROM filtered_data WHERE {group_by} = ul.{group_by} LIMIT 1) as shop_name,
                COUNT(DISTINCT ul.request_id) as total_leads,
                COUNT(DISTINCT CASE WHEN fd.outcome IS NOT NULL AND fd.outcome != 'NO_ANSWER' THEN ul.request_id END) as picked_calls,
                (
                    SELECT jsonb_object_agg(outcome, outcome_count)
                    FROM outcome_counts oc
                    WHERE oc.{group_by} = ul.{group_by}
                ) as outcome_counts
            FROM unique_leads ul
            LEFT JOIN filtered_data fd ON ul.request_id = fd.request_id AND ul.{group_by} = fd.{group_by}
            GROUP BY ul.{group_by}
            ORDER BY total_leads DESC;
        """
    else:
        # Aggregate lead-based analytics (original behavior)
        text = f"""
            WITH filtered_data AS (
                SELECT
                    lct.request_id,
                    lct.status,
                    lct.outcome
                FROM "{LEAD_CALL_TRACKER_TABLE}" lct
                {join_clause}
                {where_clause}
            ),
            base_leads AS (
                SELECT
                    request_id,
                    COUNT(*) as total_calls,
                    COUNT(*) FILTER (WHERE status = 'FINISHED') as finished_calls,
                    COUNT(*) FILTER (WHERE outcome = 'NO_ANSWER') as no_answer_calls,
                    COUNT(*) FILTER (WHERE outcome IS NOT NULL) as calls_with_outcome
                FROM filtered_data
                GROUP BY request_id
            ),
            outcome_leads AS (
                SELECT
                    request_id,
                    jsonb_object_agg(
                        COALESCE(outcome, 'N/A'),
                        outcome_count
                    ) as outcome_breakdown
                FROM (
                    SELECT
                        request_id,
                        outcome,
                        COUNT(*) as outcome_count
                    FROM filtered_data
                    GROUP BY request_id, outcome
                ) grouped_outcomes
                GROUP BY request_id
            )
            SELECT
                base_leads.request_id,
                base_leads.total_calls,
                base_leads.finished_calls,
                base_leads.no_answer_calls,
                base_leads.calls_with_outcome,
                COALESCE(outcome_leads.outcome_breakdown, '{{}}'::jsonb) as outcome_breakdown
            FROM base_leads
            LEFT JOIN outcome_leads ON base_leads.request_id = outcome_leads.request_id;
        """

    return text, values


def get_analytics_lead_based_trends_query(
    filters: Dict[str, Any], time_granularity: str = "day"
) -> Tuple[str, List[Any]]:
    """
    Generate query for lead-based trends analytics with time bucketing.
    Groups by unique request_id (lead) within each time bucket.

    Returns one row per unique lead per time bucket with outcome breakdown.
    """
    conditions, values = build_analytics_where_clause(filters)
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    # Add LEFT JOIN only if provider filter is present
    join_clause = (
        f'LEFT JOIN "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id'
        if "provider" in filters and filters["provider"]
        else ""
    )

    # Determine date truncation based on granularity
    if time_granularity == "week":
        date_trunc = "week"
    elif time_granularity == "month":
        date_trunc = "month"
    else:
        date_trunc = "day"

    # Add call_initiated_time NOT NULL condition
    extra_condition = (
        "lct.call_initiated_time IS NOT NULL AND lct.request_id IS NOT NULL"
    )
    if where_clause:
        where_clause = f"{where_clause} AND {extra_condition}"
    else:
        where_clause = f" WHERE {extra_condition}"

    text = f"""
        WITH filtered_data AS (
            SELECT
                lct.request_id,
                lct.call_initiated_time,
                lct.status,
                lct.outcome
            FROM "{LEAD_CALL_TRACKER_TABLE}" lct
            {join_clause}
            {where_clause}
        ),
        base_lead_trends AS (
            SELECT
                DATE_TRUNC('{date_trunc}', filtered_data.call_initiated_time) as time_bucket,
                filtered_data.request_id,
                COUNT(*) as total_calls_for_lead,
                COUNT(*) FILTER (WHERE filtered_data.status = 'FINISHED') as finished_calls
            FROM filtered_data
            GROUP BY 1, 2
        ),
        lead_aggregates AS (
            SELECT
                time_bucket,
                COUNT(DISTINCT request_id) as total_leads,
                SUM(total_calls_for_lead) as total_calls,
                SUM(finished_calls) as finished_calls
            FROM base_lead_trends
            GROUP BY time_bucket
        ),
        outcome_trends AS (
            SELECT
                time_bucket,
                jsonb_object_agg(
                    COALESCE(outcome, 'N/A'),
                    outcome_count
                ) as outcome_breakdown
            FROM (
                SELECT
                    DATE_TRUNC('{date_trunc}', filtered_data.call_initiated_time) as time_bucket,
                    filtered_data.outcome,
                    COUNT(DISTINCT filtered_data.request_id) as outcome_count
                FROM filtered_data
                GROUP BY 1, 2
            ) grouped_outcomes
            GROUP BY time_bucket
        )
        SELECT
            lead_aggregates.time_bucket,
            lead_aggregates.total_leads,
            lead_aggregates.total_calls,
            lead_aggregates.finished_calls,
            COALESCE(outcome_trends.outcome_breakdown, '{{}}'::jsonb) as outcome_breakdown
        FROM lead_aggregates
        LEFT JOIN outcome_trends ON lead_aggregates.time_bucket = outcome_trends.time_bucket
        ORDER BY lead_aggregates.time_bucket ASC;
    """

    return text, values


def get_analytics_outbound_numbers_query(
    filters: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """
    Generate query for outbound numbers analytics.
    """
    conditions, values = build_analytics_where_clause(filters)

    # Build WHERE clause from lct conditions
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    # Add the NOT NULL condition for outbound number
    if where_clause:
        where_clause = f"{where_clause} AND ou.number IS NOT NULL"
    else:
        where_clause = " WHERE ou.number IS NOT NULL"

    text = f"""
        SELECT
            ou.id,
            ou.number,
            ou.provider,
            ou.status,
            ou.channels,
            ou.maximum_channels,
            COUNT(*) as total_calls,
            COUNT(*) FILTER (WHERE lct.outcome != 'NO_ANSWER' AND lct.outcome IS NOT NULL) as calls_picked,
            COUNT(*) FILTER (WHERE lct.outcome = 'NO_ANSWER') as calls_no_answer
        FROM "{LEAD_CALL_TRACKER_TABLE}" lct
        LEFT JOIN "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id
        {where_clause}
        GROUP BY ou.id, ou.number, ou.provider, ou.status, ou.channels, ou.maximum_channels
        ORDER BY total_calls DESC;
    """

    return text, values
