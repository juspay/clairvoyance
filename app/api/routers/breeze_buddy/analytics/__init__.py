"""
Analytics router with RBAC enforcement.
Single flexible POST endpoint for all analytics queries with hierarchical merchant + shop access control.
"""

from typing import Awaitable, Callable, Dict

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.logger import logger
from app.database.accessor.breeze_buddy.analytics.evaluation_result import (
    get_topics_for_source,
)
from app.schemas import (
    AnalyticsRequest,
    AnalyticsResponse,
    AnalyticsType,
    UserInfo,
)
from app.schemas.breeze_buddy.conversation_analysis import (
    ConversationTopicsResponse,
)

from .handlers import (
    download_call_details,
    get_attempts_to_connect_analytics,
    get_call_based_analytics,
    get_call_details_analytics,
    get_call_details_grouped_analytics,
    get_calls_by_hour_analytics,
    get_chat_based_analytics,
    get_chats_by_hour_analytics,
    get_conversion_analytics,
    get_distinct_merchant_ids,
    get_distinct_outcomes,
    get_distinct_resellers,
    get_lead_based_analytics,
    get_lead_status_counts,
    get_outcome_counts,
    get_performance_analytics,
    get_telephony_numbers_analytics,
    get_topic_conversations_analytics,
    get_topic_dashboard_analytics,
)
from .rbac import (
    apply_hierarchical_filters,
    get_accessible_resellers_and_merchants,
)

router = APIRouter()

# Dispatch registry: maps analytics type to its handler function.
# CALL_DETAILS_DOWNLOAD is excluded — it returns StreamingResponse directly.
_ANALYTICS_HANDLERS: Dict[AnalyticsType, Callable[..., Awaitable]] = {
    AnalyticsType.CALL_BASED: get_call_based_analytics,
    AnalyticsType.CALL_DETAILS: get_call_details_analytics,
    AnalyticsType.CALL_DETAILS_GROUPED: get_call_details_grouped_analytics,
    AnalyticsType.CHAT_BASED: get_chat_based_analytics,
    AnalyticsType.LEAD_BASED: get_lead_based_analytics,
    AnalyticsType.LEAD_STATUS_COUNTS: get_lead_status_counts,
    AnalyticsType.TELEPHONY_NUMBERS: get_telephony_numbers_analytics,
    AnalyticsType.CONVERSION: get_conversion_analytics,
    AnalyticsType.PERFORMANCE: get_performance_analytics,
    AnalyticsType.DISTINCT_OUTCOMES: get_distinct_outcomes,
    AnalyticsType.OUTCOME_COUNTS: get_outcome_counts,
    AnalyticsType.DISTINCT_RESELLERS: get_distinct_resellers,
    AnalyticsType.DISTINCT_MERCHANT_IDS: get_distinct_merchant_ids,
    AnalyticsType.ATTEMPTS_TO_CONNECT: get_attempts_to_connect_analytics,
    AnalyticsType.CALLS_BY_HOUR: get_calls_by_hour_analytics,
    AnalyticsType.CHATS_BY_HOUR: get_chats_by_hour_analytics,
    AnalyticsType.TOPIC_DASHBOARD: get_topic_dashboard_analytics,
    AnalyticsType.TOPIC_CONVERSATIONS: get_topic_conversations_analytics,
}


@router.post(
    "/analytics",
    response_model=AnalyticsResponse,
    responses={200: {"content": {"text/csv": {}}}},
)
async def get_analytics(
    request: AnalyticsRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Single flexible analytics endpoint with RBAC enforcement.

    Supports multiple analytics types:
    - call-based: Aggregate or time-series call statistics (use time_granularity for trends)
    - call-details: Paginated call records with all fields
    - lead-based: Aggregate or time-series lead statistics (use time_granularity for trends)
    - telephony-numbers: Per-number telephony analytics (calls, pickup, capacity)
    - conversion: Conversion funnel metrics
    - performance: Performance metrics

    Security:
    - Automatically filters data by user's accessible merchants and shops (from JWT)
    - Admin users can access all data
    - Non-admin users can only access their authorized merchants/shops
    - Access control cannot be bypassed via request parameters

    Example Request:
        {
            "type": "call-based",
            "filters": {
                "template": "order-confirmation",
                "date_from": "2025-12-01",
                "date_to": "2025-12-31"
            },
            "options": {
                "page": 1,
                "limit": 50
            }
        }
    """
    try:
        # Convert filters to dict (exclude None values)
        filters = request.filters.model_dump(exclude_none=True)
        options = request.options.model_dump()

        # Apply hierarchical RBAC filtering
        filters = apply_hierarchical_filters(filters, current_user)

        logger.info(
            f"Analytics request from {current_user.username} (role: {current_user.role}): "
            f"type={request.type}, filters={filters}"
        )

        # Route to appropriate handler based on analytics type
        if request.type == AnalyticsType.CALL_DETAILS_DOWNLOAD:
            return await download_call_details(filters, options, current_user)

        handler = _ANALYTICS_HANDLERS.get(request.type)
        if not handler:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown analytics type: {request.type}",
            )

        data = await handler(filters, options, current_user)

        return AnalyticsResponse(success=True, data=data)

    except HTTPException:
        raise
    except NotImplementedError as e:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(e))
    except Exception as e:
        logger.error(f"Error processing analytics request: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing analytics request: {str(e)}",
        )


@router.get(
    "/analytics/conversation-topics/{source_id}",
    response_model=ConversationTopicsResponse,
)
async def get_conversation_topics(
    source_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> ConversationTopicsResponse:
    """Return extracted topics for a single conversation (voice lead or chat session).

    source_id is the lead_call_tracker.id for voice or chat_session.id (as text) for chat.
    Returns an empty topics list when no completed analysis exists.
    """
    reseller_ids, merchant_ids = get_accessible_resellers_and_merchants(current_user)
    return ConversationTopicsResponse(
        topics=await get_topics_for_source(
            source_id,
            reseller_ids,
            merchant_ids,
        )
    )
