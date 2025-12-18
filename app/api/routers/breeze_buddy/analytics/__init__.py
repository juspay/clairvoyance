"""
Analytics router with RBAC enforcement.
Single flexible POST endpoint for all analytics queries with hierarchical merchant + shop access control.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.logger import logger
from app.schemas import (
    AnalyticsRequest,
    AnalyticsResponse,
    AnalyticsType,
    UserInfo,
)

from .handlers import (
    get_call_details_analytics,
    get_conversion_analytics,
    get_lead_based_analytics,
    get_outbound_numbers_analytics,
    get_performance_analytics,
    get_summary_analytics,
    get_trends_analytics,
)
from .rbac import apply_hierarchical_filters

router = APIRouter()


@router.post("/analytics", response_model=AnalyticsResponse)
async def get_analytics(
    request: AnalyticsRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Single flexible analytics endpoint with RBAC enforcement.

    Supports multiple analytics types:
    - summary: Aggregate statistics with outcome breakdowns
    - call-details: Paginated call records with all fields
    - lead-based: Analytics by unique lead (not by call attempt)
    - outbound-numbers: Analytics grouped by outbound number
    - trends: Time-series data
    - conversion: Conversion funnel metrics
    - performance: Performance metrics

    Security:
    - Automatically filters data by user's accessible merchants and shops (from JWT)
    - Admin users can access all data
    - Non-admin users can only access their authorized merchants/shops
    - Access control cannot be bypassed via request parameters

    Example Request:
        {
            "type": "summary",
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
        filters = request.filters.dict(exclude_none=True)
        options = request.options.dict()

        # Apply hierarchical RBAC filtering
        filters = apply_hierarchical_filters(filters, current_user)

        logger.info(
            f"Analytics request from {current_user.username} (role: {current_user.role}): "
            f"type={request.type}, filters={filters}"
        )

        # Route to appropriate handler based on analytics type
        if request.type == AnalyticsType.SUMMARY:
            data = await get_summary_analytics(filters, options, current_user)
        elif request.type == AnalyticsType.CALL_DETAILS:
            data = await get_call_details_analytics(filters, options, current_user)
        elif request.type == AnalyticsType.LEAD_BASED:
            data = await get_lead_based_analytics(filters, options, current_user)
        elif request.type == AnalyticsType.OUTBOUND_NUMBERS:
            data = await get_outbound_numbers_analytics(filters, options, current_user)
        elif request.type == AnalyticsType.TRENDS:
            data = await get_trends_analytics(filters, options, current_user)
        elif request.type == AnalyticsType.CONVERSION:
            data = await get_conversion_analytics(filters, options, current_user)
        elif request.type == AnalyticsType.PERFORMANCE:
            data = await get_performance_analytics(filters, options, current_user)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown analytics type: {request.type}"
            )

        return AnalyticsResponse(
            success=True,
            data=data
        )

    except HTTPException:
        raise
    except NotImplementedError as e:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Error processing analytics request: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing analytics request: {str(e)}"
        )
