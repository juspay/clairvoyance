"""
Analytics handlers for different analytics types.
All handlers use database-level filtering for optimal scalability.
"""

import json
from datetime import datetime, timedelta
from typing import Any, Dict

from app.database.accessor.breeze_buddy.analytics import (
    get_analytics_count_from_db,
    get_call_details_from_db,
    get_lead_based_analytics_from_db,
    get_lead_based_trends_from_db,
    get_outbound_numbers_analytics_from_db,
    get_summary_analytics_from_db,
    get_trends_analytics_from_db,
)
from app.schemas import CallDetailResult, UserInfo
from app.utils.common import parse_json


def parse_outcome_breakdown(outcome_breakdown: Any) -> Dict[str, int]:
    """
    Parse outcome_breakdown from database which can be:
    - A dict (already deserialized JSONB)
    - A JSON string (needs parsing)
    - None or empty

    Returns:
        Dict with outcome counts, or empty dict if invalid/empty
    """
    if not outcome_breakdown:
        return {}

    # If it's already a dict, return it
    if isinstance(outcome_breakdown, dict):
        return outcome_breakdown

    # If it's a string (JSON), parse it
    if isinstance(outcome_breakdown, str):
        try:
            parsed = json.loads(outcome_breakdown)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass

    # Default to empty dict if we can't parse it
    return {}


async def get_call_based_analytics(
    filters: Dict[str, Any],
    options: Dict[str, Any],
    current_user: UserInfo
) -> Dict[str, Any]:
    """
    Get call-based analytics with outcome breakdowns.
    Supports both aggregate (no time_granularity) and time-series (with time_granularity).
    All aggregations done at database level.

    Response structure:
    - If time_granularity is None: Returns single aggregate in results array
    - If time_granularity is provided: Returns time-series buckets in results array
    """
    time_granularity = options.get("time_granularity")

    if time_granularity:
        # Time-series mode: Get trends data
        trend_data_from_db = await get_trends_analytics_from_db(filters, time_granularity)

        # Format results for response
        results = []
        for row in trend_data_from_db:
            time_bucket = row["time_bucket"]
            total_calls = row["total_calls"] or 0
            completed_calls = row["completed_calls"] or 0
            failed_calls = total_calls - completed_calls
            success_rate = (completed_calls / total_calls * 100) if total_calls > 0 else 0.0

            data_point = {
                "total_calls": total_calls,
                "completed_calls": completed_calls,
                "failed_calls": failed_calls,
                "success_rate": round(success_rate, 2),
                "average_duration": round(float(row["average_duration"]), 2) if row["average_duration"] else None,
                "outcome_breakdown": parse_outcome_breakdown(row.get("outcome_breakdown"))
            }

            if time_granularity == "day":
                data_point["date"] = time_bucket.date().isoformat()
            elif time_granularity == "week":
                # Calculate week start (already truncated to Monday by PostgreSQL)
                week_start = time_bucket.date()
                week_end = week_start + timedelta(days=6)
                iso_cal = week_start.isocalendar()
                data_point["week"] = f"{iso_cal[0]}-W{iso_cal[1]:02d}"
                data_point["week_start"] = week_start.isoformat()
                data_point["week_end"] = week_end.isoformat()
            elif time_granularity == "month":
                data_point["month"] = time_bucket.strftime("%Y-%m")
                data_point["month_name"] = time_bucket.strftime("%B %Y")

            results.append(data_point)

        return {
            "type": "call-based",
            "filters_applied": filters,
            "time_granularity": time_granularity,
            "results": results
        }
    else:
        # Aggregate mode: Get summary data
        group_by = options.get("group_by")
        summary = await get_summary_analytics_from_db(filters, group_by)

        # If group_by is used, summary is already a list; otherwise wrap in array
        if isinstance(summary, list):
            results = summary
        else:
            results = [summary]

        return {
            "type": "call-based",
            "filters_applied": filters,
            "time_granularity": None,
            "results": results
        }


async def get_call_details_analytics(
    filters: Dict[str, Any], options: Dict[str, Any], current_user: UserInfo
) -> Dict[str, Any]:
    """
    Get paginated call details with database-level filtering and pagination.
    """
    page = options.get("page", 1)
    limit = options.get("limit", 50)
    sort_by = options.get("sort_by", "created_at")
    sort_order = options.get("sort_order", "desc")

    offset = (page - 1) * limit

    # Get total count from database
    total = await get_analytics_count_from_db(filters)

    # Get paginated call details from database
    trackers = await get_call_details_from_db(
        filters=filters,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    total_pages = (total + limit - 1) // limit if limit > 0 else 0

    # Convert to CallDetailResult
    results = []
    for tracker in trackers:
        # Calculate duration
        duration = None
        if tracker.get("call_initiated_time") and tracker.get("call_end_time"):
            duration = int(
                (
                    tracker["call_end_time"] - tracker["call_initiated_time"]
                ).total_seconds()
            )

        # Parse payload and metadata (handles both string and dict)
        payload = parse_json(tracker, "payload")
        metadata = parse_json(tracker, "meta_data")

        # Extract transcription - it might be nested in metadata
        transcript = None
        if metadata:
            # Try to get transcription field
            transcription_data = metadata.get("transcription")
            if transcription_data:
                # If it's a dict, try to extract the actual transcript text
                if isinstance(transcription_data, dict):
                    # Look for common transcript field names
                    transcript = transcription_data.get("transcript") or transcription_data.get("text") or transcription_data.get("content")
                elif isinstance(transcription_data, str):
                    transcript = transcription_data

        results.append(CallDetailResult(
            call_id=tracker.get("call_id") or tracker["id"],
            lead_id=tracker["id"],
            order_id=tracker.get("request_id"),
            template=tracker["template"],
            merchant_id=tracker["merchant_id"],
            shop_identifier=tracker.get("shop_identifier"),
            shop_name=payload.get("shop_name") if payload else None,
            customer_name=payload.get("customer_name") if payload else None,
            customer_phone=payload.get("phone") if payload else None,
            customer_mobile_number=payload.get("customer_mobile_number") if payload else None,
            status=tracker.get("status", "UNKNOWN"),
            outcome=tracker.get("outcome") if tracker.get("outcome") else "N/A",
            duration=duration,
            recording_url=tracker.get("recording_url"),
            transcript=transcript,
            calling_provider=tracker.get("calling_provider"),
            attempt_count=tracker.get("attempt_count"),
            cost=tracker.get("cost"),
            created_at=tracker.get("call_initiated_time") or tracker.get("created_at") or datetime.now(),
            updated_at=tracker.get("updated_at")
        ))

    return {
        "type": "call-details",
        "filters_applied": filters,
        "results": [r.dict() for r in results],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": total_pages,
        },
    }


async def get_lead_based_analytics(
    filters: Dict[str, Any], options: Dict[str, Any], current_user: UserInfo
) -> Dict[str, Any]:
    """
    Get lead-based analytics (counts by unique lead).
    Supports both aggregate (no time_granularity) and time-series (with time_granularity).
    All aggregations done at database level.

    Response structure:
    - If time_granularity is None: Returns single aggregate in results array
    - If time_granularity is provided: Returns time-series buckets in results array
    """
    time_granularity = options.get("time_granularity")

    if time_granularity:
        # Time-series mode: Get lead-based trends data
        trend_data_from_db = await get_lead_based_trends_from_db(filters, time_granularity)

        # Format results for response
        results = []
        for row in trend_data_from_db:
            time_bucket = row["time_bucket"]
            total_leads = row["total_leads"] or 0
            total_calls = row["total_calls"] or 0
            finished_calls = row["finished_calls"] or 0
            outcome_breakdown = parse_outcome_breakdown(row.get("outcome_breakdown"))

            # Calculate picked_calls (leads that answered)
            no_answer_count = 0
            for outcome, count in outcome_breakdown.items():
                outcome_lower = str(outcome).lower()
                if 'no_answer' in outcome_lower or 'no answer' in outcome_lower or outcome_lower == 'noanswer':
                    no_answer_count += count

            picked_calls = total_leads - no_answer_count

            data_point = {
                "total_leads": total_leads,
                "total_calls": total_calls,
                "finished_calls": finished_calls,
                "picked_calls": picked_calls,
                "outcome_counts": outcome_breakdown
            }

            if time_granularity == "day":
                data_point["date"] = time_bucket.date().isoformat()
            elif time_granularity == "week":
                # Calculate week start (already truncated to Monday by PostgreSQL)
                week_start = time_bucket.date()
                week_end = week_start + timedelta(days=6)
                iso_cal = week_start.isocalendar()
                data_point["week"] = f"{iso_cal[0]}-W{iso_cal[1]:02d}"
                data_point["week_start"] = week_start.isoformat()
                data_point["week_end"] = week_end.isoformat()
            elif time_granularity == "month":
                data_point["month"] = time_bucket.strftime("%Y-%m")
                data_point["month_name"] = time_bucket.strftime("%B %Y")

            results.append(data_point)

        return {
            "type": "lead-based",
            "filters_applied": filters,
            "time_granularity": time_granularity,
            "results": results
        }
    else:
        # Aggregate mode: Get lead-based summary data
        group_by = options.get("group_by")
        lead_data = await get_lead_based_analytics_from_db(filters, group_by)

        if group_by:
            # Grouped results - data is already aggregated by database
            results = []
            for row in lead_data:
                results.append({
                    group_by: row[group_by],
                    "shop_name": row.get("shop_name"),
                    "total_leads": row["total_leads"] or 0,
                    "picked_calls": row["picked_calls"] or 0,
                    "outcome_counts": parse_outcome_breakdown(row.get("outcome_counts"))
                })

            return {
                "type": "lead-based",
                "filters_applied": filters,
                "time_granularity": None,
                "results": results
            }
        else:
            # Aggregate (original behavior)
            # Calculate high-level metrics
            total_leads = len(lead_data)

            # Dynamic outcome counting (template-agnostic)
            outcome_counts = {}
            no_answer_count = 0
            for lead in lead_data:
                outcome_breakdown = parse_outcome_breakdown(lead.get("outcome_breakdown"))
                for outcome, count in outcome_breakdown.items():
                    if count > 0:
                        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
                        # Track NO_ANSWER leads (case-insensitive)
                        outcome_lower = str(outcome).lower()
                        if 'no_answer' in outcome_lower or 'no answer' in outcome_lower or outcome_lower == 'noanswer':
                            no_answer_count += 1

            # picked_calls = total_leads - NO_ANSWER
            picked_calls = total_leads - no_answer_count

            lead_based = {
                "total_leads": total_leads,
                "picked_calls": picked_calls,
                "outcome_counts": outcome_counts
            }

            return {
                "type": "lead-based",
                "filters_applied": filters,
                "time_granularity": None,
                "results": [lead_based]  # Wrap in array for consistency
            }


async def get_outbound_numbers_analytics(
    filters: Dict[str, Any], options: Dict[str, Any], current_user: UserInfo
) -> Dict[str, Any]:
    """
    Get analytics grouped by outbound number.
    All aggregations done at database level.
    """
    # Get outbound numbers analytics from database
    outbound_data = await get_outbound_numbers_analytics_from_db(filters)

    outbound_analytics = []
    for record in outbound_data:
        outbound_analytics.append({
            "id": record["id"],
            "number": record["number"],
            "provider": record["provider"],
            "status": record["status"],
            "channels": record.get("channels"),
            "maximum_channels": record.get("maximum_channels"),
            "total_calls": record["total_calls"],
            "calls_picked": record["calls_picked"],
            "calls_no_answer": record["calls_no_answer"],
        })

    return {
        "type": "outbound-numbers",
        "filters_applied": filters,
        "results": outbound_analytics,
    }


async def get_conversion_analytics(
    filters: Dict[str, Any], options: Dict[str, Any], current_user: UserInfo
) -> Dict[str, Any]:
    """
    Get conversion funnel analytics.

    Analyzes the conversion funnel from call initiation to completion.
    Shows drop-off points and conversion rates at each stage.
    """
    # Get summary analytics for conversion calculation
    summary = await get_summary_analytics_from_db(filters)

    # Define conversion funnel stages using actual fields from summary
    total_initiated = summary.get("total_calls", 0)
    completed_calls = summary.get("completed_calls", 0)
    summary.get("failed_calls", 0)

    # Get outcome breakdown to extract specific outcome counts
    outcome_breakdown = parse_outcome_breakdown(summary.get("outcome_breakdown"))

    # Extract calls_no_answer from outcome_breakdown (case-insensitive lookup)
    calls_no_answer = 0
    for outcome, count in outcome_breakdown.items():
        outcome_lower = str(outcome).lower()
        if (
            "no_answer" in outcome_lower
            or "no answer" in outcome_lower
            or outcome_lower == "noanswer"
        ):
            calls_no_answer += count

    # Calculate funnel stages
    # Connected = completed + no_answer (calls that reached the customer)
    total_connected = completed_calls + calls_no_answer
    total_completed = completed_calls

    # Build funnel stages
    funnel_stages = [
        {"stage": "initiated", "count": total_initiated, "percentage": 100.0},
        {
            "stage": "connected",
            "count": total_connected,
            "percentage": (
                (total_connected / total_initiated * 100)
                if total_initiated > 0
                else 0.0
            ),
        },
        {
            "stage": "completed",
            "count": total_completed,
            "percentage": (
                (total_completed / total_initiated * 100)
                if total_initiated > 0
                else 0.0
            ),
        },
    ]

    # Add outcome-based stages
    for outcome, count in outcome_breakdown.items():
        if count > 0:
            funnel_stages.append(
                {
                    "stage": outcome.lower().replace(" ", "_"),
                    "count": count,
                    "percentage": (
                        (count / total_initiated * 100) if total_initiated > 0 else 0.0
                    ),
                }
            )

    # Calculate conversion rate (initiated to completed)
    conversion_rate = (
        (total_completed / total_initiated * 100) if total_initiated > 0 else 0.0
    )

    # Calculate drop-off points
    drop_off_points = []

    # Drop-off from initiated to connected
    initiated_to_connected_dropoff = total_initiated - total_connected
    if initiated_to_connected_dropoff > 0:
        drop_off_points.append(
            {
                "stage": "initiated_to_connected",
                "drop_off": initiated_to_connected_dropoff,
                "drop_off_rate": (
                    (initiated_to_connected_dropoff / total_initiated * 100)
                    if total_initiated > 0
                    else 0.0
                ),
            }
        )

    # Drop-off from connected to completed
    connected_to_completed_dropoff = total_connected - total_completed
    if connected_to_completed_dropoff > 0:
        drop_off_points.append(
            {
                "stage": "connected_to_completed",
                "drop_off": connected_to_completed_dropoff,
                "drop_off_rate": (
                    (connected_to_completed_dropoff / total_connected * 100)
                    if total_connected > 0
                    else 0.0
                ),
            }
        )

    conversion_data = {
        "total_initiated": total_initiated,
        "total_connected": total_connected,
        "total_completed": total_completed,
        "funnel_stages": funnel_stages,
        "conversion_rate": round(conversion_rate, 2),
        "drop_off_points": drop_off_points,
    }

    return {
        "type": "conversion",
        "filters_applied": filters,
        "results": conversion_data,
    }


async def get_performance_analytics(
    filters: Dict[str, Any], options: Dict[str, Any], current_user: UserInfo
) -> Dict[str, Any]:
    """
    Get performance metrics analytics.

    Provides performance metrics including success rates, average duration,
    cost efficiency, and outcome distribution.
    """
    # Get summary analytics for performance calculation
    summary = await get_summary_analytics_from_db(filters)

    total_calls = summary.get("total_calls", 0)
    failed_calls = summary.get("failed_calls", 0)

    # Get outcome breakdown to extract specific outcome counts
    outcome_breakdown = parse_outcome_breakdown(summary.get("outcome_breakdown"))

    # Extract specific outcomes from breakdown (case-insensitive lookup)
    calls_no_answer = 0
    calls_busy = 0
    for outcome, count in outcome_breakdown.items():
        outcome_lower = str(outcome).lower()
        if (
            "no_answer" in outcome_lower
            or "no answer" in outcome_lower
            or outcome_lower == "noanswer"
        ):
            calls_no_answer += count
        elif "busy" in outcome_lower:
            calls_busy += count

    # calls_picked = total_calls - NO_ANSWER
    calls_picked = total_calls - calls_no_answer

    # Use success_rate from summary (already calculated)
    success_rate = summary.get("success_rate", 0.0)

    # Calculate answer rate (calls picked + no answer / total calls)
    answer_rate = (
        ((calls_picked + calls_no_answer) / total_calls * 100)
        if total_calls > 0
        else 0.0
    )

    # Calculate failure rate
    failure_rate = (failed_calls / total_calls * 100) if total_calls > 0 else 0.0

    # Get average duration from summary
    avg_duration = summary.get("average_duration")

    # Get total cost if available (not currently provided by summary)
    total_cost = summary.get("total_cost", 0)

    # Calculate cost per successful call
    cost_per_success = (
        (total_cost / calls_picked) if calls_picked > 0 and total_cost > 0 else 0.0
    )

    # Calculate outcome distribution percentages
    outcome_distribution = {}
    for outcome, count in outcome_breakdown.items():
        outcome_distribution[outcome] = {
            "count": count,
            "percentage": (count / total_calls * 100) if total_calls > 0 else 0.0,
        }

    # Build performance metrics
    performance_data = {
        "total_calls": total_calls,
        "success_rate": round(success_rate, 2),
        "answer_rate": round(answer_rate, 2),
        "failure_rate": round(failure_rate, 2),
        "average_duration": round(avg_duration, 2) if avg_duration else None,
        "total_cost": round(total_cost, 2) if total_cost else None,
        "cost_per_success": round(cost_per_success, 2) if cost_per_success else None,
        "call_breakdown": {
            "picked": calls_picked,
            "no_answer": calls_no_answer,
            "busy": calls_busy,
            "failed": failed_calls,
        },
        "outcome_distribution": outcome_distribution,
    }

    return {
        "type": "performance",
        "filters_applied": filters,
        "results": performance_data,
    }
