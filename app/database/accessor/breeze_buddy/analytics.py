"""
Database accessor functions for analytics with generic filtering.
All queries are optimized to filter at database level.
"""

from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.analytics import (
    get_analytics_call_details_query,
    get_analytics_count_query,
    get_analytics_inbound_count_query,
    get_analytics_lead_based_query,
    get_analytics_lead_based_trends_query,
    get_analytics_lead_status_counts_query,
    get_analytics_lead_status_counts_total_query,
    get_analytics_outbound_numbers_query,
    get_analytics_summary_query,
    get_analytics_trends_query,
    get_call_details_records_query,
)


async def get_summary_analytics_from_db(
    filters: Dict[str, Any], group_by: Optional[str] = None
) -> Any:
    """
    Get summary analytics with all aggregations done at database level.

    Args:
        filters: Analytics filters
        group_by: Optional field to group by (returns list if provided, dict otherwise)

    Returns:
        List of dicts if group_by is provided, single dict otherwise
    """
    logger.info(
        f"[Analytics DB] Getting summary analytics with filters: {filters}, group_by: {group_by}"
    )

    try:
        query_text, values = get_analytics_summary_query(filters, group_by)
        result = await run_parameterized_query(query_text, values)

        logger.debug(
            f"[Analytics DB] Summary query returned {len(result) if result else 0} rows"
        )

        if not result or len(result) == 0:
            logger.warning("[Analytics DB] Summary query returned no results")
            if group_by:
                return []
            return {
                "total_calls": 0,
                "completed_calls": 0,
                "failed_calls": 0,
                "success_rate": 0.0,
                "average_duration": None,
                "total_templates": 0,
                "total_shops": 0,
                "outcome_breakdown": {},
            }

        if group_by:
            # Return list of grouped results
            grouped_results = []
            for row in result:
                total_calls = row["total_calls"] or 0
                completed_calls = row["completed_calls"] or 0
                failed_calls = row["failed_calls"] or 0
                success_rate = (
                    (completed_calls / total_calls * 100) if total_calls > 0 else 0.0
                )

                grouped_results.append(
                    {
                        group_by: row[group_by],
                        "shop_name": row.get("shop_name"),
                        "total_calls": total_calls,
                        "completed_calls": completed_calls,
                        "failed_calls": failed_calls,
                        "success_rate": round(success_rate, 2),
                        "average_duration": (
                            round(float(row["average_duration"]), 2)
                            if row["average_duration"]
                            else None
                        ),
                        "total_templates": row["total_templates"] or 0,
                        "total_shops": row["total_shops"] or 0,
                        "outcome_breakdown": row["outcome_breakdown"] or {},
                    }
                )

            logger.info(
                f"[Analytics DB] Grouped summary returned {len(grouped_results)} groups"
            )
            return grouped_results
        else:
            # Return single aggregate result
            row = result[0]
            total_calls = row["total_calls"] or 0
            completed_calls = row["completed_calls"] or 0
            failed_calls = row["failed_calls"] or 0
            success_rate = (
                (completed_calls / total_calls * 100) if total_calls > 0 else 0.0
            )

            logger.info(
                f"[Analytics DB] Summary result: {total_calls} total calls, {completed_calls} completed ({success_rate:.2f}% success)"
            )

            return {
                "total_calls": total_calls,
                "completed_calls": completed_calls,
                "failed_calls": failed_calls,
                "success_rate": round(success_rate, 2),
                "average_duration": (
                    round(float(row["average_duration"]), 2)
                    if row["average_duration"]
                    else None
                ),
                "total_templates": row["total_templates"] or 0,
                "total_shops": row["total_shops"] or 0,
                "outcome_breakdown": row["outcome_breakdown"] or {},
            }

    except Exception as e:
        logger.error(f"Error getting summary analytics: {e}", exc_info=True)
        raise


async def get_call_details_from_db(
    filters: Dict[str, Any],
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> List[Dict[str, Any]]:
    """
    Get paginated call details with database-level filtering.

    Returns:
        List of call detail records
    """
    logger.info(
        f"[Analytics DB] Getting call details with filters: {filters}, limit: {limit}, offset: {offset}"
    )

    try:
        query_text, values = get_analytics_call_details_query(
            filters, limit, offset, sort_by, sort_order
        )
        result = await run_parameterized_query(query_text, values)
        logger.info(
            f"[Analytics DB] Call details returned {len(result) if result else 0} records"
        )
        return [dict(row) for row in result] if result else []

    except Exception as e:
        logger.error(f"Error getting call details: {e}", exc_info=True)
        raise


async def get_call_detail_records(
    filters: Dict[str, Any],
    sort_by: str = "call_initiated_time",
    sort_order: str = "desc",
    limit: int = 0,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """
    Get call details matching filters for CSV download.
    When limit > 0, fetches a single batch (for streaming).
    When limit == 0, fetches all matching records.

    Returns:
        List of matching call detail records
    """
    logger.info(f"[Analytics DB] Getting call details download with filters: {filters}")

    try:
        query_text, values = get_call_details_records_query(
            filters, sort_by, sort_order, limit, offset
        )
        result = await run_parameterized_query(query_text, values)
        logger.info(
            f"[Analytics DB] Call details download returned {len(result) if result else 0} records"
        )
        return [dict(row) for row in result] if result else []

    except Exception as e:
        logger.error(f"Error getting call details download: {e}", exc_info=True)
        raise


async def get_analytics_count_from_db(filters: Dict[str, Any]) -> int:
    """
    Get count of records matching filters.

    Returns:
        Total count
    """
    logger.debug(f"[Analytics DB] Getting analytics count with filters: {filters}")

    try:
        query_text, values = get_analytics_count_query(filters)
        result = await run_parameterized_query(query_text, values)

        count = result[0]["count"] if result and len(result) > 0 else 0
        logger.debug(f"[Analytics DB] Count result: {count}")
        return count or 0

    except Exception as e:
        logger.error(f"Error getting analytics count: {e}", exc_info=True)
        raise


async def get_trends_analytics_from_db(
    filters: Dict[str, Any], time_granularity: str = "day"
) -> List[Dict[str, Any]]:
    """
    Get time-series trends with aggregations done at database level.

    Returns:
        List of trend data points (one per time bucket)
    """
    logger.info(
        f"[Analytics DB] Getting trends analytics with filters: {filters}, granularity: {time_granularity}"
    )

    try:
        query_text, values = get_analytics_trends_query(filters, time_granularity)
        result = await run_parameterized_query(query_text, values)
        logger.info(
            f"[Analytics DB] Trends returned {len(result) if result else 0} time buckets"
        )
        return [dict(row) for row in result] if result else []

    except Exception as e:
        logger.error(f"Error getting trends analytics: {e}", exc_info=True)
        raise


async def get_lead_based_analytics_from_db(
    filters: Dict[str, Any], group_by: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Get lead-based analytics (one row per unique lead/request_id).

    Args:
        filters: Analytics filters
        group_by: Optional field to group by (returns grouped results if provided)

    Returns:
        List of lead analytics records
    """
    logger.info(
        f"[Analytics DB] Getting lead-based analytics with filters: {filters}, group_by: {group_by}"
    )

    try:
        query_text, values = get_analytics_lead_based_query(filters, group_by)
        result = await run_parameterized_query(query_text, values)
        logger.info(
            f"[Analytics DB] Lead-based analytics returned {len(result) if result else 0} {'groups' if group_by else 'leads'}"
        )
        return [dict(row) for row in result] if result else []

    except Exception as e:
        logger.error(f"Error getting lead-based analytics: {e}", exc_info=True)
        raise


async def get_inbound_count_from_db(
    filters: Dict[str, Any], group_by: Optional[str] = None
) -> Any:
    """
    Get inbound call counts with database-level filtering.

    Args:
        filters: Analytics filters
        group_by: Optional field to group by (returns list if provided, int otherwise)

    Returns:
        int (aggregate count) or List[Dict] (grouped counts)
    """
    logger.info(
        f"[Analytics DB] Getting inbound count with filters: {filters}, group_by: {group_by}"
    )

    # Normalize group_by to prevent query/accessor divergence
    allowed_group_by_fields = {
        "template",
        "reseller_id",
        "merchant_id",
    }
    if group_by and group_by not in allowed_group_by_fields:
        logger.warning(
            f"[Analytics DB] Invalid group_by field '{group_by}', falling back to aggregate mode"
        )
        group_by = None

    try:
        query_text, values = get_analytics_inbound_count_query(filters, group_by)
        result = await run_parameterized_query(query_text, values)

        if not result or len(result) == 0:
            logger.warning("[Analytics DB] Inbound count query returned no results")
            return [] if group_by else 0

        if group_by:
            # Return list of grouped results with inbound_calls count
            grouped_results = [
                {
                    group_by: row[group_by],
                    "inbound_calls": row["inbound_calls"] or 0,
                }
                for row in result
            ]
            logger.info(
                f"[Analytics DB] Inbound count returned {len(grouped_results)} groups"
            )
            return grouped_results
        else:
            # Return single aggregate count
            count = result[0]["inbound_calls"] or 0
            logger.info(f"[Analytics DB] Inbound count result: {count}")
            return count

    except Exception as e:
        logger.error(f"Error getting inbound count: {e}", exc_info=True)
        raise


async def get_outbound_numbers_analytics_from_db(
    filters: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Get outbound numbers analytics.

    Returns:
        List of outbound number statistics
    """
    logger.info(
        f"[Analytics DB] Getting outbound numbers analytics with filters: {filters}"
    )

    try:
        query_text, values = get_analytics_outbound_numbers_query(filters)
        result = await run_parameterized_query(query_text, values)
        logger.info(
            f"[Analytics DB] Outbound numbers analytics returned {len(result) if result else 0} numbers"
        )
        return [dict(row) for row in result] if result else []

    except Exception as e:
        logger.error(f"Error getting outbound numbers analytics: {e}", exc_info=True)
        raise


async def get_lead_based_trends_from_db(
    filters: Dict[str, Any], time_granularity: str = "day"
) -> List[Dict[str, Any]]:
    """
    Get lead-based time-series trends with aggregations done at database level.

    Returns:
        List of trend data points (one per time bucket) with lead counts
    """
    logger.info(
        f"[Analytics DB] Getting lead-based trends with filters: {filters}, granularity: {time_granularity}"
    )

    try:
        query_text, values = get_analytics_lead_based_trends_query(
            filters, time_granularity
        )
        result = await run_parameterized_query(query_text, values)
        logger.info(
            f"[Analytics DB] Lead-based trends returned {len(result) if result else 0} time buckets"
        )
        return [dict(row) for row in result] if result else []

    except Exception as e:
        logger.error(f"Error getting lead-based trends: {e}", exc_info=True)
        raise


async def get_lead_status_counts_from_db(
    filters: Dict[str, Any],
    page: int = 1,
    limit: int = 10,
    search_reseller_id: Optional[str] = None,
    search_merchant_identifier: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Get lead status counts with pagination and search.
    Always groups by reseller and merchant, sorted by total_count DESC.

    Args:
        filters: Analytics filters (merchant_ids, reseller_ids, date range, etc.)
        page: Page number (1-indexed)
        limit: Number of rows per page
        search_reseller_id: Partial reseller_id to search for (case-insensitive)
        search_merchant_identifier: Partial merchant_id to search for (case-insensitive)

    Returns:
        Dict with 'results' (list of counts) and 'pagination' (total, page, limit, total_pages)
    """
    logger.info(
        f"[Analytics DB] Getting lead status counts with filters: {filters}, "
        f"page: {page}, limit: {limit}, "
        f"search_reseller_id: {search_reseller_id}, search_merchant_identifier: {search_merchant_identifier}"
    )

    try:
        # Get paginated data
        query_text, values = get_analytics_lead_status_counts_query(
            filters, page, limit, search_reseller_id, search_merchant_identifier
        )
        result = await run_parameterized_query(query_text, values)

        # Get total count for pagination
        total_query_text, total_values = get_analytics_lead_status_counts_total_query(
            filters, search_reseller_id, search_merchant_identifier
        )
        total_result = await run_parameterized_query(total_query_text, total_values)
        total_count = total_result[0]["total"] if total_result else 0

        total_pages = (total_count + limit - 1) // limit if limit > 0 else 1

        logger.info(
            f"[Analytics DB] Lead status counts returned {len(result) if result else 0} rows, "
            f"total: {total_count}, total_pages: {total_pages}"
        )

        return {
            "results": [dict(row) for row in result] if result else [],
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total_count,
                "total_pages": total_pages,
            },
        }

    except Exception as e:
        logger.error(f"Error getting lead status counts: {e}", exc_info=True)
        raise
