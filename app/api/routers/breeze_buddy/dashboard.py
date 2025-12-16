from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import RedirectResponse
from starlette.responses import FileResponse, JSONResponse

from app.ai.voice.agents.breeze_buddy.managers.calls import (
    process_backlog_leads,
)
from app.core.logger import logger
from app.core.security.jwt import get_breeze_buddy_session, get_current_user
from app.database.accessor import (
    get_all_lead_call_trackers,
    get_all_outbound_numbers_with_call_count,
    get_lead_based_analytics,
    get_lead_call_trackers_count,
)
from app.schemas import TokenData

router = APIRouter()


@router.get("/dashboard", include_in_schema=False)
async def get_dashboard(session: dict = Depends(get_breeze_buddy_session)):
    """
    Serves the dashboard HTML file.
    """
    if not session:
        return RedirectResponse(url="/agent/voice/breeze-buddy/login")
    response = FileResponse("app/ai/voice/agents/breeze_buddy/dashboard/index.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@router.get("/cron/initiate")
async def initiate_cron(
    background_tasks: BackgroundTasks,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Initiates the cron job to process backlog leads.
    """
    logger.info(f"Authenticated user {current_user.user_id} initiating cron job")
    background_tasks.add_task(process_backlog_leads)
    return {"status": "success", "message": "Lead processing initiated"}


@router.get("/breeze/order-confirmation/analytics", include_in_schema=False)
async def get_analytics(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    session: dict = Depends(get_breeze_buddy_session),
):
    """
    Provides analytics data for the dashboard with both call-based and lead-based metrics.
    """
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        start_datetime = (
            datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
            if start_date
            else None
        )
        end_datetime = (
            (
                datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
                + timedelta(days=1)
            )
            if end_date
            else None
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date format. Expected ISO format (YYYY-MM-DD): {str(e)}",
        ) from e

    trackers = await get_all_lead_call_trackers(
        start_date=start_datetime, end_date=end_datetime
    )

    # Call-based analytics (counts every call)
    call_based = {
        "calls_attempted": len(
            [t for t, _ in trackers if t.status and t.status.value == "FINISHED"]
        ),
        "no_answer": len(
            [t for t, _ in trackers if t.outcome and t.outcome == "NO_ANSWER"]
        ),
        "connected_and_busy": len(
            [t for t, _ in trackers if t.outcome and t.outcome == "BUSY"]
        ),
        "address_confirmed": len(
            [t for t, _ in trackers if t.outcome and t.outcome == "CONFIRM"]
        ),
        "order_cancelled": len(
            [t for t, _ in trackers if t.outcome and t.outcome == "CANCEL"]
        ),
        "address_updated": len(
            [t for t, _ in trackers if t.outcome and t.outcome == "ADDRESS_UPDATED"]
        ),
    }
    # Get all lead details for lead-based analytics

    lead_data = await get_lead_based_analytics(
        start_date=start_datetime, end_date=end_datetime
    )

    lead_based = {
        "calls_attempted": len(lead_data),
        "picked_calls": len(
            [
                lead
                for lead in lead_data
                if lead["finished_calls"] > lead["no_answer_calls"]
            ]
        ),
        "confirmed_address": len(
            [lead for lead in lead_data if lead["confirmed_calls"] > 0]
        ),
        "requested_cancellation": len(
            [lead for lead in lead_data if lead["cancelled_calls"] > 0]
        ),
        "address_updated": len(
            [lead for lead in lead_data if lead["address_update_calls"] > 0]
        ),
    }

    # Get outbound number analytics
    outbound_numbers_data = await get_all_outbound_numbers_with_call_count(
        start_date=start_datetime,
        end_date=end_datetime,
    )

    outbound_analytics = []
    for record in outbound_numbers_data:
        calls_picked = record["total_calls"] - record["calls_no_answer"]
        outbound_analytics.append(
            {
                "number": record["number"],
                "provider": record["provider"],
                "total_calls": record["total_calls"],
                "calls_picked": calls_picked,
            }
        )

    analytics = {
        "call_based": call_based,
        "lead_based": lead_based,
        "outbound_numbers": outbound_analytics,
    }

    return JSONResponse(content=analytics)


@router.get("/breeze/order-confirmation/call-details", include_in_schema=False)
async def get_call_details(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page: int = 1,
    page_size: int = 10,
    outcome: Optional[str] = None,
    order_id: Optional[str] = None,
    shop_name: Optional[str] = None,
    session: dict = Depends(get_breeze_buddy_session),
):
    """
    Provides paginated call details for the dashboard.
    """
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Validate pagination parameters
    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=400, detail="Page size must be between 1 and 100"
        )

    try:
        start_datetime = (
            datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
            if start_date
            else None
        )
        end_datetime = (
            (
                datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
                + timedelta(days=1)
            )
            if end_date
            else None
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date format. Expected ISO format (YYYY-MM-DD): {str(e)}",
        ) from e

    total_items = await get_lead_call_trackers_count(
        start_date=start_datetime,
        end_date=end_datetime,
        outcome=outcome,
        request_id=order_id,
        shop_name=shop_name,
    )

    trackers = await get_all_lead_call_trackers(
        start_date=start_datetime,
        end_date=end_datetime,
        outcome=outcome,
        request_id=order_id,
        shop_name=shop_name,
        page=page,
        page_size=page_size,
    )

    total_pages = (total_items + page_size - 1) // page_size

    items = []
    for t, calling_provider in trackers:
        items.append(
            {
                "id": t.id,
                "order_id": t.request_id,
                "customer_name": t.payload.get("customer_name"),
                "shop_name": t.payload.get("shop_name"),
                "customer_mobile_number": t.payload.get("customer_mobile_number"),
                "outcome": t.outcome if t.outcome else "N/A",
                "created_at": t.call_initiated_time,
                "call_id": t.call_id,
                "recording_url": t.recording_url,
                "transcript": (t.metaData.get("transcription") if t.metaData else None),
                "calling_provider": calling_provider,
                "attempt_count": t.attempt_count,
            }
        )

    return {
        "total_items": total_items,
        "total_pages": total_pages,
        "page": page,
        "page_size": page_size,
        "items": items,
    }
