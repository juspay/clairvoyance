"""
Generic analytics query builder for template-agnostic analytics.
All filtering is done at database level for optimal performance.
"""

import re
import uuid as uuid_module
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.core.logger import logger

# Table names
LEAD_CALL_TRACKER_TABLE = "lead_call_tracker"
OUTBOUND_NUMBER_TABLE = "outbound_number"

# Pattern for valid JSONB key names (alphanumeric, underscore, hyphen only)
# This prevents SQL injection via malicious key names in payload filters
VALID_JSONB_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def escape_ilike_pattern(value: str) -> str:
    """
    Escape ILIKE special characters for literal matching.

    ILIKE uses % and _ as wildcards:
    - % matches any sequence of characters
    - _ matches any single character

    To match these characters literally, we escape them with backslash.
    We also escape backslash itself to handle it correctly.

    Args:
        value: The string to escape

    Returns:
        Escaped string safe for ILIKE pattern matching
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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


IST = ZoneInfo("Asia/Kolkata")


def convert_ist_to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    else:
        dt = dt.astimezone(IST)

    return dt.astimezone(timezone.utc)


def build_analytics_where_clause(
    filters: Dict[str, Any],
    value_offset: int = 0,
    filter_execution_mode: bool = True,
    date_column: str = "call_initiated_time",
) -> Tuple[List[str], List[Any]]:
    """
    Build WHERE clause conditions and values from generic filters.

    Args:
        filters: Dictionary of filter key-value pairs
        value_offset: Starting index for parameterized query values
        filter_execution_mode: If True, only include TELEPHONY execution_mode (exclude tests)
        date_column: Column to use for date range filtering (default: call_initiated_time).
                     Use "created_at" for queries that need to include leads without calls (e.g. BACKLOG).

    Returns:
        Tuple of (conditions list, values list)
    """
    conditions = []
    values = []

    # Filter by execution_mode to exclude test calls from analytics.
    # HOLD_TRANSFER is included so hold & consult outbound legs appear in
    # the dashboard alongside regular TELEPHONY calls.
    if filter_execution_mode:
        conditions.append("lct.execution_mode IN ('TELEPHONY', 'HOLD_TRANSFER')")

    # Date range filters - convert IST to UTC before passing to DB
    if "date_from" in filters and filters["date_from"]:
        date_from = filters["date_from"]
        if isinstance(date_from, datetime):
            values.append(convert_ist_to_utc(date_from))
        else:
            values.append(
                convert_ist_to_utc(datetime.combine(date_from, datetime.min.time()))
            )
        conditions.append(f"lct.{date_column} >= ${len(values) + value_offset}")

    if "date_to" in filters and filters["date_to"]:
        date_to = filters["date_to"]
        if isinstance(date_to, datetime):
            values.append(convert_ist_to_utc(date_to))
        else:
            next_day_start_ist = datetime.combine(
                date_to, datetime.min.time()
            ) + timedelta(days=1)
            values.append(convert_ist_to_utc(next_day_start_ist))
        conditions.append(f"lct.{date_column} < ${len(values) + value_offset}")

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

    if "reseller_id" in filters and filters["reseller_id"]:
        values.append(filters["reseller_id"])
        conditions.append(f"lct.reseller_id = ${len(values) + value_offset}")

    if "reseller_ids" in filters and filters["reseller_ids"]:
        # Use ANY for array matching
        values.append(filters["reseller_ids"])
        conditions.append(f"lct.reseller_id = ANY(${len(values) + value_offset})")

    if "merchant_id" in filters and filters["merchant_id"]:
        values.append(filters["merchant_id"])
        conditions.append(f"lct.merchant_id = ${len(values) + value_offset}")

    if "merchant_ids" in filters and filters["merchant_ids"]:
        values.append(filters["merchant_ids"])
        conditions.append(f"lct.merchant_id = ANY(${len(values) + value_offset})")

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
        group_by: Optional field to group by (e.g., 'merchant_id', 'template')

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
    allowed_group_by_fields = [
        "merchant_id",
        "template",
        "reseller_id",
    ]
    if group_by and group_by not in allowed_group_by_fields:
        group_by = None

    if group_by:
        # Grouped analytics — materialised CTE so the base scan is done once and
        # reused by both the aggregate and the outcome-breakdown subquery.
        # Payload, template and total_shops are omitted: they are not consumed by
        # the Loom frontend and force expensive JSONB / TOAST access.
        text = f"""
            WITH filtered_data AS MATERIALIZED (
                SELECT
                    lct.status,
                    lct.outcome,
                    lct.call_initiated_time,
                    lct.call_end_time,
                    lct.call_direction,
                    lct.merchant_id
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
            ),
            outcome_per_group AS (
                SELECT
                    {group_by},
                    jsonb_object_agg(COALESCE(outcome, 'N/A'), outcome_count) as outcome_breakdown
                FROM outcome_groups
                GROUP BY {group_by}
            ),
            group_stats AS (
                SELECT
                    fd.{group_by},
                    COUNT(*) as total_calls,
                    COUNT(*) FILTER (WHERE fd.call_direction = 'OUTBOUND') as outbound_calls,
                    COUNT(*) FILTER (WHERE fd.call_direction = 'INBOUND') as inbound_calls,
                    COUNT(*) FILTER (WHERE fd.status = 'FINISHED') as completed_calls,
                    COUNT(*) FILTER (WHERE fd.status != 'FINISHED' OR fd.status IS NULL) as failed_calls,
                    AVG(
                        EXTRACT(EPOCH FROM (fd.call_end_time - fd.call_initiated_time))
                    ) FILTER (
                        WHERE fd.call_initiated_time IS NOT NULL
                        AND fd.call_end_time IS NOT NULL
                    ) as average_duration
                FROM filtered_data fd
                GROUP BY fd.{group_by}
            )
            SELECT
                gs.*,
                COALESCE(opg.outcome_breakdown, '{{}}'::jsonb) as outcome_breakdown
            FROM group_stats gs
            LEFT JOIN outcome_per_group opg ON gs.{group_by} = opg.{group_by}
            ORDER BY total_calls DESC;
        """
    else:
        # Aggregate analytics — counts only.  Outcome breakdown is fetched via
        # a separate parallel query (get_analytics_outcome_breakdown_query).
        # AVG(duration) and COUNT(DISTINCT) are skipped — they are not consumed
        # by the dashboard cards and dominate query time on large date ranges.
        text = f"""
            WITH filtered_data AS NOT MATERIALIZED (
                SELECT
                    lct.status,
                    lct.call_direction
                FROM "{LEAD_CALL_TRACKER_TABLE}" lct
                {join_clause}
                {where_clause}
            )
            SELECT
                COUNT(*) as total_calls,
                COUNT(*) FILTER (WHERE call_direction = 'OUTBOUND') as outbound_calls,
                COUNT(*) FILTER (WHERE call_direction = 'INBOUND') as inbound_calls,
                COUNT(*) FILTER (WHERE status = 'FINISHED') as completed_calls,
                COUNT(*) FILTER (WHERE status != 'FINISHED' OR status IS NULL) as failed_calls
            FROM filtered_data;
        """

    return text, values


def get_analytics_outcome_breakdown_query(
    filters: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """
    Standalone query for outcome-breakdown only.

    Intended to run in parallel with the slimmed-down summary query so the
    jsonb_object_agg (which requires a GROUP BY) doesn't serialise behind the
    main aggregates.
    """
    conditions, values = build_analytics_where_clause(filters)
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    join_clause = (
        f'LEFT JOIN "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id'
        if "provider" in filters and filters["provider"]
        else ""
    )

    text = f"""
        SELECT
            COALESCE(
                jsonb_object_agg(COALESCE(outcome, 'N/A'), outcome_count),
                '{{}}'::jsonb
            ) as outcome_breakdown
        FROM (
            SELECT
                lct.outcome,
                COUNT(*) as outcome_count
            FROM "{LEAD_CALL_TRACKER_TABLE}" lct
            {join_clause}
            {where_clause}
            GROUP BY lct.outcome
        ) grouped_outcomes;
    """
    return text, values


def get_analytics_call_details_query(
    filters: Dict[str, Any],
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "call_initiated_time",
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
            f"[Analytics Query] Invalid sort column '{sort_by}', defaulting to 'call_initiated_time'"
        )
        sort_by = "call_initiated_time"

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


def get_call_details_records_query(
    filters: Dict[str, Any],
    sort_by: str = "call_initiated_time",
    sort_order: str = "desc",
    limit: int = 0,
    offset: int = 0,
) -> Tuple[str, List[Any]]:
    """
    Generate query for call details download.
    When limit > 0, applies LIMIT/OFFSET for batched streaming.
    When limit == 0, returns all matching records.
    """
    conditions, values = build_analytics_where_clause(
        filters, filter_execution_mode=True
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
        sort_by = "call_initiated_time"

    sort_direction = "DESC" if sort_order.lower() == "desc" else "ASC"

    pagination_clause = ""
    if limit > 0:
        pagination_clause = f"LIMIT ${len(values) + 1} OFFSET ${len(values) + 2}"
        values.extend([limit, offset])

    text = f"""
        SELECT
            lct.id,
            lct.call_id,
            lct.template,
            lct.payload,
            lct.meta_data->>'outcome' as meta_data_outcome,
            lct.call_initiated_time,
            lct.call_end_time,
            lct.outcome,
            lct.attempt_count + 1 as attempt_count,
            ou.provider as calling_provider
        FROM "{LEAD_CALL_TRACKER_TABLE}" lct
        LEFT JOIN "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id
        {where_clause}
        ORDER BY lct.{sort_by} {sort_direction}
        {pagination_clause};
    """

    return text, values


def get_analytics_count_query(
    filters: Dict[str, Any], filter_execution_mode: bool = True
) -> Tuple[str, List[Any]]:
    """
    Generate query to count records matching filters.
    """
    conditions, values = build_analytics_where_clause(
        filters, filter_execution_mode=filter_execution_mode
    )
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
        group_by: Optional field to group by (e.g., 'merchant_id', 'template')

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
    allowed_group_by_fields = [
        "merchant_id",
        "template",
        "reseller_id",
    ]
    if group_by and group_by not in allowed_group_by_fields:
        group_by = None

    if group_by:
        # Grouped lead-based analytics — materialised CTE so the base scan is
        # done once.  Payload/shop_name are omitted (not consumed by Loom).
        # request_id is effectively unique per row (1 duplicate out of 1M),
        # so COUNT(*) is equivalent to COUNT(DISTINCT request_id) and allows
        # HashAggregate instead of expensive sort-based distinct counting.
        text = f"""
            WITH filtered_data AS MATERIALIZED (
                SELECT
                    lct.{group_by},
                    lct.call_direction,
                    lct.status,
                    lct.outcome
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
                WHERE outcome IS NOT NULL
                GROUP BY {group_by}, outcome
            ),
            outcome_per_group AS (
                SELECT
                    {group_by},
                    jsonb_object_agg(outcome, outcome_count) as outcome_counts
                FROM outcome_groups
                GROUP BY {group_by}
            ),
            group_stats AS (
                SELECT
                    fd.{group_by},
                    COUNT(*) as total_leads,
                    COUNT(*) FILTER (WHERE fd.call_direction = 'OUTBOUND') as outbound_leads,
                    COUNT(*) FILTER (WHERE fd.call_direction = 'INBOUND') as inbound_leads,
                    COUNT(*) FILTER (WHERE fd.outcome IS NOT NULL AND fd.outcome != 'NO_ANSWER') as picked_calls
                FROM filtered_data fd
                GROUP BY fd.{group_by}
            )
            SELECT
                gs.*,
                COALESCE(opg.outcome_counts, '{{}}'::jsonb) as outcome_counts
            FROM group_stats gs
            LEFT JOIN outcome_per_group opg ON gs.{group_by} = opg.{group_by}
            ORDER BY total_leads DESC;
        """
    else:
        # Aggregate lead-based analytics — pre-aggregated in SQL (returns 1 row).
        # Per-request_id grouping is eliminated: request_id is effectively unique
        # per row, so row-level COUNT(*) FILTER gives the same result as the
        # original two-pass aggregation.
        text = f"""
            WITH filtered_data AS MATERIALIZED (
                SELECT
                    lct.call_direction,
                    lct.status,
                    lct.outcome
                FROM "{LEAD_CALL_TRACKER_TABLE}" lct
                {join_clause}
                {where_clause}
            ),
            lead_aggregates AS (
                SELECT
                    COUNT(*) as total_leads,
                    COUNT(*) FILTER (WHERE call_direction = 'OUTBOUND') as outbound_leads,
                    COUNT(*) FILTER (WHERE call_direction = 'INBOUND') as inbound_leads,
                    COUNT(*) FILTER (
                        WHERE status = 'FINISHED'
                        AND outcome IS DISTINCT FROM 'NO_ANSWER'
                    ) as picked_calls
                FROM filtered_data
            ),
            outcome_counts AS (
                SELECT
                    outcome,
                    COUNT(*) as lead_count
                FROM filtered_data
                WHERE outcome IS NOT NULL
                GROUP BY outcome
            ),
            outcome_agg AS (
                SELECT
                    COALESCE(
                        jsonb_object_agg(outcome, lead_count),
                        '{{}}'::jsonb
                    ) as outcome_counts
                FROM outcome_counts
            )
            SELECT
                lead_aggregates.total_leads,
                lead_aggregates.outbound_leads,
                lead_aggregates.inbound_leads,
                lead_aggregates.picked_calls,
                outcome_agg.outcome_counts
            FROM lead_aggregates
            CROSS JOIN outcome_agg;
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


def get_analytics_lead_status_counts_query(
    filters: Dict[str, Any],
    page: int = 1,
    limit: int = 10,
    search_reseller_id: Optional[str] = None,
    search_merchant_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query for lead status counts with pagination and search.
    Returns counts grouped by reseller and merchant_id, sorted by total_count DESC.

    Args:
        filters: Analytics filters (reseller_id, reseller_ids, date range, etc.)
        page: Page number (1-indexed)
        limit: Number of rows per page
        search_reseller_id: Partial reseller_id to search for (case-insensitive)
        search_merchant_id: Partial merchant_id to search for (case-insensitive)

    Returns:
        Tuple of (query_string, values_list)
    """
    conditions, values = build_analytics_where_clause(
        filters, filter_execution_mode=True, date_column="created_at"
    )

    # Add search conditions for reseller_id
    if search_reseller_id:
        conditions.append(f"lct.reseller_id ILIKE ${len(values) + 1} ESCAPE '\\'")
        values.append(f"%{escape_ilike_pattern(search_reseller_id)}%")

    # Add search conditions for merchant_id
    if search_merchant_id:
        conditions.append(f"lct.merchant_id ILIKE ${len(values) + 1} ESCAPE '\\'")
        values.append(f"%{escape_ilike_pattern(search_merchant_id)}%")

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    # Calculate offset (convert 1-indexed page to 0-indexed offset)
    offset = (page - 1) * limit

    # Main query with pagination - groups by reseller_id and merchant_id
    text = f"""
        SELECT
            lct.reseller_id,
            lct.merchant_id,
            COUNT(*) FILTER (WHERE lct.status = 'BACKLOG') as backlog_count,
            COUNT(*) FILTER (WHERE lct.status = 'PROCESSING') as processing_count,
            COUNT(*) FILTER (WHERE lct.status = 'FINISHED') as finished_count,
            COUNT(*) as total_count
        FROM "{LEAD_CALL_TRACKER_TABLE}" lct
        {where_clause}
        GROUP BY lct.reseller_id, lct.merchant_id
        ORDER BY total_count DESC
        LIMIT ${len(values) + 1} OFFSET ${len(values) + 2};
    """
    values.append(limit)
    values.append(offset)

    return text, values


def get_analytics_lead_status_counts_total_query(
    filters: Dict[str, Any],
    search_reseller_id: Optional[str] = None,
    search_merchant_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """
    Generate query to get total count of lead status groups for pagination.
    Returns the total number of reseller/merchant combinations matching filters.

    Args:
        filters: Analytics filters
        search_reseller_id: Partial reseller_id to search for
        search_merchant_id: Partial merchant_id to search for

    Returns:
        Tuple of (query_string, values_list)
    """
    conditions, values = build_analytics_where_clause(
        filters, filter_execution_mode=True, date_column="created_at"
    )

    # Add search conditions for reseller_id
    if search_reseller_id:
        conditions.append(f"lct.reseller_id ILIKE ${len(values) + 1} ESCAPE '\\'")
        values.append(f"%{escape_ilike_pattern(search_reseller_id)}%")

    # Add search conditions for merchant_id
    if search_merchant_id:
        conditions.append(f"lct.merchant_id ILIKE ${len(values) + 1} ESCAPE '\\'")
        values.append(f"%{escape_ilike_pattern(search_merchant_id)}%")

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    text = f"""
        SELECT COUNT(*) as total
        FROM (
            SELECT 1
            FROM "{LEAD_CALL_TRACKER_TABLE}" lct
            {where_clause}
            GROUP BY lct.reseller_id, lct.merchant_id
        ) as grouped;
    """

    return text, values


def get_distinct_outcomes_query(
    filters: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """
    Returns a list of distinct outcome values from lead_call_tracker,
    filtered by date range and RBAC constraints.
    Used by the frontend to populate the dynamic outcome filter dropdown.
    """
    conditions, values = build_analytics_where_clause(
        filters, filter_execution_mode=True
    )
    conditions.append("lct.outcome IS NOT NULL")

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    # Add LEFT JOIN only if provider filter is present
    join_clause = (
        f'LEFT JOIN "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id'
        if "provider" in filters and filters["provider"]
        else ""
    )

    query = f"""
        SELECT DISTINCT lct.outcome
        FROM "{LEAD_CALL_TRACKER_TABLE}" lct
        {join_clause}
        {where_clause}
        ORDER BY lct.outcome ASC;
    """

    return query, values


def get_outcome_counts_query(
    filters: Dict[str, Any],
    page: int = 1,
    limit: int = 10,
) -> Tuple[str, List[Any]]:
    """
    Returns outcome values with their call counts from lead_call_tracker.
    Counts ALL rows (not unique leads). Grouped by outcome.
    Paginated with LIMIT/OFFSET.
    Used by the Outcome Analytics table on the analytics page.
    """
    conditions, values = build_analytics_where_clause(
        filters, filter_execution_mode=True
    )
    conditions.append("lct.outcome IS NOT NULL")

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    # Add LEFT JOIN only if provider filter is present
    join_clause = (
        f'LEFT JOIN "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id'
        if "provider" in filters and filters["provider"]
        else ""
    )

    offset = (page - 1) * limit
    limit_param = f"${len(values) + 1}"
    offset_param = f"${len(values) + 2}"

    query = f"""
        SELECT
            lct.outcome,
            COUNT(*) as call_count
        FROM "{LEAD_CALL_TRACKER_TABLE}" lct
        {join_clause}
        {where_clause}
        GROUP BY lct.outcome
        ORDER BY call_count DESC
        LIMIT {limit_param} OFFSET {offset_param};
    """

    values.append(limit)
    values.append(offset)

    return query, values


def get_outcome_counts_total_query(
    filters: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """
    Returns the total number of distinct outcomes matching the filters.
    Used for pagination metadata (total_pages calculation).
    """
    conditions, values = build_analytics_where_clause(
        filters, filter_execution_mode=True
    )
    conditions.append("lct.outcome IS NOT NULL")

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    # Add LEFT JOIN only if provider filter is present
    join_clause = (
        f'LEFT JOIN "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id'
        if "provider" in filters and filters["provider"]
        else ""
    )

    query = f"""
        SELECT COUNT(*) as total
        FROM (
            SELECT 1
            FROM "{LEAD_CALL_TRACKER_TABLE}" lct
            {join_clause}
            {where_clause}
            GROUP BY lct.outcome
        ) as grouped;
    """

    return query, values


def get_distinct_resellers_query(
    filters: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """
    Returns distinct reseller_ids from lead_call_tracker.
    Used for the Reseller filter dropdown on the Records page.
    """
    conditions, values = build_analytics_where_clause(
        filters, filter_execution_mode=True
    )
    conditions.append("lct.reseller_id IS NOT NULL")

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    join_clause = (
        f'LEFT JOIN "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id'
        if "provider" in filters and filters["provider"]
        else ""
    )

    query = f"""
        SELECT DISTINCT lct.reseller_id as reseller_id
        FROM "{LEAD_CALL_TRACKER_TABLE}" lct
        {join_clause}
        {where_clause}
        ORDER BY reseller_id ASC;
    """

    return query, values


def get_dashboard_counts_query(
    filters: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """
    Lightweight query for dashboard card counts only.

    Reuses build_analytics_where_clause() but avoids CTEs, JOINs,
    AVG, COUNT(DISTINCT), and jsonb_object_agg for minimal scan cost.
    """
    conditions, values = build_analytics_where_clause(filters)
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    text = f"""
        SELECT
            COUNT(*) FILTER (WHERE lct.call_direction = 'OUTBOUND') as outbound_calls,
            COUNT(*) FILTER (WHERE lct.call_direction = 'INBOUND') as inbound_calls,
            COUNT(*) FILTER (WHERE lct.outcome = 'NO_ANSWER') as no_answer_calls,
            COUNT(*) FILTER (WHERE lct.outcome = 'BUSY') as busy_calls,
            COUNT(*) FILTER (WHERE lct.status = 'FINISHED' AND lct.outcome IS NOT NULL) as calls_with_outcomes
        FROM "{LEAD_CALL_TRACKER_TABLE}" lct
        {where_clause};
    """
    return text, values


def get_distinct_merchant_ids_query(
    filters: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """
    Returns distinct merchant_ids from lead_call_tracker.
    Optionally scoped to a specific reseller via reseller_id filter.
    Used for the Merchant ID filter dropdown on the Records page.
    """
    conditions, values = build_analytics_where_clause(
        filters, filter_execution_mode=True
    )
    conditions.append("lct.merchant_id IS NOT NULL")

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    join_clause = (
        f'LEFT JOIN "{OUTBOUND_NUMBER_TABLE}" ou ON lct.outbound_number_id = ou.id'
        if "provider" in filters and filters["provider"]
        else ""
    )

    query = f"""
        SELECT DISTINCT lct.merchant_id as merchant_id
        FROM "{LEAD_CALL_TRACKER_TABLE}" lct
        {join_clause}
        {where_clause}
        ORDER BY merchant_id ASC;
    """

    return query, values
