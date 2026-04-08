"""
Analytics service layer — all business logic for analytics endpoints.
Database access is delegated to the accessor layer.
"""

import csv
import io
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from starlette.responses import StreamingResponse

from app.database.accessor.breeze_buddy.analytics import (
    get_analytics_count_from_db,
    get_call_detail_records,
    get_call_details_from_db,
    get_distinct_merchant_ids_from_db,
    get_distinct_outcomes_from_db,
    get_distinct_resellers_from_db,
    get_lead_based_analytics_from_db,
    get_lead_based_trends_from_db,
    get_lead_status_counts_from_db,
    get_outbound_numbers_analytics_from_db,
    get_outcome_counts_from_db,
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

    if isinstance(outcome_breakdown, dict):
        return outcome_breakdown

    if isinstance(outcome_breakdown, str):
        try:
            parsed = json.loads(outcome_breakdown)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass

    return {}


def _format_time_bucket(
    data_point: Dict[str, Any], time_bucket, time_granularity: str
) -> None:
    """Apply day/week/month formatting to a time-series data point (mutates in place)."""
    if time_granularity == "day":
        data_point["date"] = time_bucket.date().isoformat()
    elif time_granularity == "week":
        week_start = time_bucket.date()
        week_end = week_start + timedelta(days=6)
        iso_cal = week_start.isocalendar()
        data_point["week"] = f"{iso_cal[0]}-W{iso_cal[1]:02d}"
        data_point["week_start"] = week_start.isoformat()
        data_point["week_end"] = week_end.isoformat()
    elif time_granularity == "month":
        data_point["month"] = time_bucket.strftime("%Y-%m")
        data_point["month_name"] = time_bucket.strftime("%B %Y")


async def get_call_based_analytics(
    filters: Dict[str, Any], options: Dict[str, Any], current_user: UserInfo
) -> Dict[str, Any]:
    """
    Get call-based analytics with outcome breakdowns.
    Supports both aggregate (no time_granularity) and time-series (with time_granularity).
    """
    time_granularity = options.get("time_granularity")

    if time_granularity:
        trend_data_from_db = await get_trends_analytics_from_db(
            filters, time_granularity
        )

        results = []
        for row in trend_data_from_db:
            total_calls = row["total_calls"] or 0
            completed_calls = row["completed_calls"] or 0
            failed_calls = total_calls - completed_calls
            success_rate = (
                (completed_calls / total_calls * 100) if total_calls > 0 else 0.0
            )

            data_point = {
                "total_calls": total_calls,
                "completed_calls": completed_calls,
                "failed_calls": failed_calls,
                "success_rate": round(success_rate, 2),
                "average_duration": (
                    round(float(row["average_duration"]), 2)
                    if row["average_duration"]
                    else None
                ),
                "outcome_breakdown": parse_outcome_breakdown(
                    row.get("outcome_breakdown")
                ),
            }

            _format_time_bucket(data_point, row["time_bucket"], time_granularity)
            results.append(data_point)

        return {
            "type": "call-based",
            "filters_applied": filters,
            "time_granularity": time_granularity,
            "results": results,
        }
    else:
        group_by = options.get("group_by")
        summary = await get_summary_analytics_from_db(filters, group_by)

        if isinstance(summary, list):
            results = summary
        else:
            results = [summary]

        return {
            "type": "call-based",
            "filters_applied": filters,
            "time_granularity": None,
            "results": results,
        }


async def get_call_details_analytics(
    filters: Dict[str, Any], options: Dict[str, Any], current_user: UserInfo
) -> Dict[str, Any]:
    """Get paginated call details with database-level filtering and pagination."""
    page = options.get("page", 1)
    limit = options.get("limit", 50)
    sort_by = options.get("sort_by", "call_initiated_time")
    sort_order = options.get("sort_order", "desc")

    offset = (page - 1) * limit

    total = await get_analytics_count_from_db(filters, filter_execution_mode=False)

    trackers = await get_call_details_from_db(
        filters=filters,
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    total_pages = (total + limit - 1) // limit if limit > 0 else 0

    results = []
    for tracker in trackers:
        duration = None
        if tracker.get("call_initiated_time") and tracker.get("call_end_time"):
            duration = int(
                (
                    tracker["call_end_time"] - tracker["call_initiated_time"]
                ).total_seconds()
            )

        payload = parse_json(tracker, "payload")
        metadata = parse_json(tracker, "meta_data")

        transcript = None
        if metadata:
            transcription_data = metadata.get("transcription")
            if transcription_data:
                if isinstance(transcription_data, dict):
                    transcript = (
                        transcription_data.get("transcript")
                        or transcription_data.get("text")
                        or transcription_data.get("content")
                    )
                elif isinstance(transcription_data, str):
                    transcript = transcription_data

        results.append(
            CallDetailResult(
                call_id=tracker.get("call_id") or tracker["id"],
                lead_id=tracker["id"],
                order_id=tracker.get("request_id"),
                template=tracker["template"],
                reseller_id=tracker["reseller_id"],
                merchant_id=tracker.get("merchant_id"),
                shop_name=payload.get("shop_name") if payload else None,
                customer_name=payload.get("customer_name") if payload else None,
                customer_phone=payload.get("phone") if payload else None,
                customer_mobile_number=(
                    payload.get("customer_mobile_number") if payload else None
                ),
                status=tracker.get("status", "UNKNOWN"),
                outcome=tracker.get("outcome") if tracker.get("outcome") else "N/A",
                duration=duration,
                recording_url=tracker.get("recording_url"),
                transcript=transcript,
                calling_provider=tracker.get("calling_provider"),
                attempt_count=tracker.get("attempt_count"),
                cost=tracker.get("cost"),
                payload=payload,
                created_at=tracker.get("call_initiated_time")
                or tracker.get("created_at")
                or datetime.now(),
                updated_at=tracker.get("updated_at"),
            )
        )

    return {
        "type": "call-details",
        "filters_applied": filters,
        "results": [r.model_dump() for r in results],
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
    """
    time_granularity = options.get("time_granularity")

    if time_granularity:
        trend_data_from_db = await get_lead_based_trends_from_db(
            filters, time_granularity
        )

        results = []
        for row in trend_data_from_db:
            total_leads = row["total_leads"] or 0
            total_calls = int(row["total_calls"] or 0)
            finished_calls = int(row["finished_calls"] or 0)
            outcome_breakdown = parse_outcome_breakdown(row.get("outcome_breakdown"))

            no_answer_count = 0
            for outcome, count in outcome_breakdown.items():
                outcome_lower = str(outcome).lower()
                if (
                    "no_answer" in outcome_lower
                    or "no answer" in outcome_lower
                    or outcome_lower == "noanswer"
                ):
                    no_answer_count += count

            picked_calls = total_leads - no_answer_count

            data_point = {
                "total_leads": total_leads,
                "total_calls": total_calls,
                "finished_calls": finished_calls,
                "picked_calls": picked_calls,
                "outcome_counts": outcome_breakdown,
            }

            _format_time_bucket(data_point, row["time_bucket"], time_granularity)
            results.append(data_point)

        return {
            "type": "lead-based",
            "filters_applied": filters,
            "time_granularity": time_granularity,
            "results": results,
        }
    else:
        group_by = options.get("group_by")
        lead_data = await get_lead_based_analytics_from_db(filters, group_by)

        if group_by:
            results = []
            for row in lead_data:
                results.append(
                    {
                        group_by: row[group_by],
                        "shop_name": row.get("shop_name"),
                        "outbound_leads": row.get("outbound_leads", 0),
                        "inbound_leads": row.get("inbound_leads", 0),
                        "total_leads": row["total_leads"] or 0,
                        "picked_calls": row["picked_calls"] or 0,
                        "outcome_counts": parse_outcome_breakdown(
                            row.get("outcome_counts")
                        ),
                    }
                )

            return {
                "type": "lead-based",
                "filters_applied": filters,
                "time_granularity": None,
                "results": results,
            }
        else:
            lead_based = {
                "outbound_leads": lead_data.get("outbound_leads", 0),
                "inbound_leads": lead_data.get("inbound_leads", 0),
                "total_leads": lead_data.get("total_leads", 0),
                "picked_calls": lead_data.get("picked_calls", 0),
                "outcome_counts": parse_outcome_breakdown(
                    lead_data.get("outcome_counts")
                ),
            }

            return {
                "type": "lead-based",
                "filters_applied": filters,
                "time_granularity": None,
                "results": [lead_based],
            }


async def get_outbound_numbers_analytics(
    filters: Dict[str, Any], options: Dict[str, Any], current_user: UserInfo
) -> Dict[str, Any]:
    """Get analytics grouped by outbound number."""
    outbound_data = await get_outbound_numbers_analytics_from_db(filters)

    outbound_analytics = []
    for record in outbound_data:
        outbound_analytics.append(
            {
                "id": record["id"],
                "number": record["number"],
                "provider": record["provider"],
                "status": record["status"],
                "channels": record.get("channels"),
                "maximum_channels": record.get("maximum_channels"),
                "total_calls": record["total_calls"],
                "calls_picked": record["calls_picked"],
                "calls_no_answer": record["calls_no_answer"],
            }
        )

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
    """
    summary = await get_summary_analytics_from_db(filters)

    total_initiated = summary.get("total_calls", 0)
    completed_calls = summary.get("completed_calls", 0)

    outcome_breakdown = parse_outcome_breakdown(summary.get("outcome_breakdown"))

    calls_no_answer = 0
    for outcome, count in outcome_breakdown.items():
        outcome_lower = str(outcome).lower()
        if (
            "no_answer" in outcome_lower
            or "no answer" in outcome_lower
            or outcome_lower == "noanswer"
        ):
            calls_no_answer += count

    total_connected = completed_calls + calls_no_answer
    total_completed = completed_calls

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

    conversion_rate = (
        (total_completed / total_initiated * 100) if total_initiated > 0 else 0.0
    )

    drop_off_points = []

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
    summary = await get_summary_analytics_from_db(filters)

    total_calls = summary.get("total_calls", 0)
    failed_calls = summary.get("failed_calls", 0)

    outcome_breakdown = parse_outcome_breakdown(summary.get("outcome_breakdown"))

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

    calls_picked = total_calls - calls_no_answer

    success_rate = summary.get("success_rate", 0.0)

    answer_rate = (
        ((calls_picked + calls_no_answer) / total_calls * 100)
        if total_calls > 0
        else 0.0
    )

    failure_rate = (failed_calls / total_calls * 100) if total_calls > 0 else 0.0

    avg_duration = summary.get("average_duration")

    total_cost = summary.get("total_cost", 0)

    cost_per_success = (
        (total_cost / calls_picked) if calls_picked > 0 and total_cost > 0 else 0.0
    )

    outcome_distribution = {}
    for outcome, count in outcome_breakdown.items():
        outcome_distribution[outcome] = {
            "count": count,
            "percentage": (count / total_calls * 100) if total_calls > 0 else 0.0,
        }

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


async def get_lead_status_counts(
    filters: Dict[str, Any], options: Dict[str, Any], current_user: UserInfo
) -> Dict[str, Any]:
    """Get lead status counts with pagination and search."""
    page = options.get("page", 1)
    limit = options.get("limit", 10)

    page = max(1, page)
    limit = max(1, min(limit, 100))

    search_reseller_id = filters.get("reseller_id")
    search_merchant_identifier = filters.get("merchant_id")

    db_filters = {
        k: v for k, v in filters.items() if k not in ["reseller_id", "merchant_id"]
    }

    if current_user.role != "admin":
        db_filters["reseller_id"] = (
            current_user.reseller_ids[0] if current_user.reseller_ids else None
        )

    result = await get_lead_status_counts_from_db(
        filters=db_filters,
        page=page,
        limit=limit,
        search_reseller_id=search_reseller_id,
        search_merchant_identifier=search_merchant_identifier,
    )

    formatted_results = []
    for row in result["results"]:
        formatted_results.append(
            {
                "reseller_id": row.get("reseller_id"),
                "merchant_id": (
                    row.get("merchant_id") if row.get("merchant_id") else None
                ),
                "backlog_count": row.get("backlog_count", 0) or 0,
                "processing_count": row.get("processing_count", 0) or 0,
                "finished_count": row.get("finished_count", 0) or 0,
                "total_count": row.get("total_count", 0) or 0,
            }
        )

    return {
        "type": "lead-status-counts",
        "filters_applied": filters,
        "pagination": result["pagination"],
        "results": formatted_results,
    }


# Static list of supported columns for CSV export
EXPORT_COLUMNS = [
    "Lead ID",
    "Call ID",
    "Template",
    "Name",
    "Mobile Number",
    "Start Time",
    "End Time",
    "Duration",
    "Outcome",
    "Metadata Outcome",
    "Attempt Count",
    "Record",
]


async def download_call_details(
    filters: Dict[str, Any], options: Dict[str, Any], current_user: UserInfo
) -> StreamingResponse:
    """
    Generate a CSV file download of all call details matching the filters.
    Streams CSV rows in batches to avoid loading everything into memory.

    Supports dynamic column selection via options["custom_columns"].
    custom_columns should contain clean display names (e.g. "Call Id").
    If not provided, all available columns are exported.
    """
    sort_by = options.get("sort_by", "call_initiated_time")
    sort_order = options.get("sort_order", "desc")
    custom_columns: Optional[List[str]] = options.get("custom_columns")

    # Resolve which columns to include, sorting by our natural EXPORT_COLUMNS order
    if custom_columns and len(custom_columns) > 0:
        # Include custom_columns only if they are in our static list, but maintain EXPORT_COLUMNS order
        selected_columns = [c for c in EXPORT_COLUMNS if c in custom_columns]
        if not selected_columns:
            selected_columns = list(EXPORT_COLUMNS)
    else:
        selected_columns = list(EXPORT_COLUMNS)

    BATCH_SIZE = 1000
    MAX_CSV_ROWS = 100_000

    async def generate_csv():
        output = io.StringIO()
        writer = csv.writer(output)

        # Write header row with selected display names
        writer.writerow(selected_columns)
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        offset = 0
        total_rows_written = 0
        while True:
            remaining = MAX_CSV_ROWS - total_rows_written
            if remaining <= 0:
                break

            batch_limit = min(BATCH_SIZE, remaining)
            trackers = await get_call_detail_records(
                filters=filters,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=batch_limit,
                offset=offset,
            )

            if not trackers:
                break

            for tracker in trackers:
                payload = parse_json(tracker, "payload") or {}
                start = tracker.get("call_initiated_time")
                end = tracker.get("call_end_time")

                # Pre-calculate all possible fields for this row
                row_data = {
                    "Lead ID": tracker.get("id", ""),
                    "Call ID": tracker.get("call_id", ""),
                    "Template": tracker.get("template", ""),
                    "Name": payload.get("customer_name", ""),
                    "Mobile Number": payload.get("customer_mobile_number")
                    or payload.get("phone", ""),
                    "Start Time": (
                        start.strftime("%Y-%m-%d %H:%M:%S")
                        if hasattr(start, "strftime")
                        else ""
                    ),
                    "End Time": (
                        end.strftime("%Y-%m-%d %H:%M:%S")
                        if hasattr(end, "strftime")
                        else ""
                    ),
                    "Duration": (
                        int((end - start).total_seconds())
                        if start
                        and end
                        and hasattr(start, "strftime")
                        and hasattr(end, "strftime")
                        else ""
                    ),
                    "Outcome": tracker.get("outcome", ""),
                    "Metadata Outcome": tracker.get("meta_data_outcome", ""),
                    "Attempt Count": tracker.get("attempt_count", ""),
                    "Record": (
                        f"https://buddy.breezelabs.app/calls/records/{tracker.get('id')}"
                        if tracker.get("id")
                        else ""
                    ),
                }

                # Select only the columns requested by the user, in consistent order
                row = [row_data.get(col, "") for col in selected_columns]
                writer.writerow(row)

            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

            total_rows_written += len(trackers)

            if len(trackers) < batch_limit:
                break

            offset += len(trackers)

    filename = f"call_details_{datetime.now().strftime('%Y-%m-%d')}.csv"

    return StreamingResponse(
        generate_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def get_distinct_outcomes(
    filters: Dict[str, Any],
    options: Dict[str, Any],
    current_user: UserInfo,
) -> Dict[str, Any]:
    """Get distinct outcome values."""
    outcomes = await get_distinct_outcomes_from_db(filters)

    return {
        "type": "distinct-outcomes",
        "filters_applied": filters,
        "results": {
            "outcomes": outcomes,
        },
    }


async def get_outcome_counts(
    filters: Dict[str, Any],
    options: Dict[str, Any],
    current_user: UserInfo,
) -> Dict[str, Any]:
    """Get paginated outcome counts."""
    page = max(1, min(options.get("page", 1), 1000))
    limit = max(1, min(options.get("limit", 10), 100))

    data = await get_outcome_counts_from_db(filters, page, limit)

    page_total_calls = data["page_total_calls"]
    results = []
    for row in data["results"]:
        call_count = row.get("call_count", 0)
        percentage = round(
            (call_count / page_total_calls * 100) if page_total_calls > 0 else 0.0, 2
        )
        results.append(
            {
                "outcome": row["outcome"],
                "count": call_count,
                "percentage": percentage,
            }
        )

    return {
        "type": "outcome-counts",
        "filters_applied": filters,
        "pagination": data["pagination"],
        "results": results,
        "page_total_calls": page_total_calls,
    }


async def get_distinct_resellers(
    filters: Dict[str, Any],
    options: Dict[str, Any],
    current_user: UserInfo,
) -> Dict[str, Any]:
    """Get distinct reseller IDs."""
    resellers = await get_distinct_resellers_from_db(filters)

    return {
        "type": "distinct-resellers",
        "filters_applied": filters,
        "results": {
            "resellers": resellers,
        },
    }


async def get_distinct_merchant_ids(
    filters: Dict[str, Any],
    options: Dict[str, Any],
    current_user: UserInfo,
) -> Dict[str, Any]:
    """Get distinct merchant IDs."""
    merchant_ids = await get_distinct_merchant_ids_from_db(filters)

    return {
        "type": "distinct-merchant-ids",
        "filters_applied": filters,
        "results": {
            "merchant_ids": merchant_ids,
        },
    }
