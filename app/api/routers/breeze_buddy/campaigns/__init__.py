"""
Campaign endpoints — named bulk lead pushes for the console.

- POST /campaigns              - Create a campaign (bulk lead push, per-row errors)
- GET  /campaigns              - List campaigns with live lead aggregates
- GET  /campaigns/{id}         - One campaign + aggregates
- POST /campaigns/{id}/stop    - Stop: abort every BACKLOG/PROCESSING lead

Per-lead analytics for a campaign ride the existing POST /analytics types via
the ``campaign_id`` filter (call-details, outcome-counts, attempts-to-connect…).
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, status

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.campaigns import (
    CampaignListResponse,
    CampaignWithStats,
    CreateCampaignRequest,
    CreateCampaignResponse,
    StopCampaignResponse,
)

from .handlers import (
    create_campaign_handler,
    get_campaign_handler,
    list_campaigns_handler,
    stop_campaign_handler,
)

router = APIRouter()


@router.post(
    "/campaigns",
    response_model=CreateCampaignResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_campaign(
    request: CreateCampaignRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Create a campaign: every lead goes through the standard push-lead
    validation; rejected rows are reported per-row, not as a batch failure."""
    return await create_campaign_handler(request, current_user)


@router.get("/campaigns", response_model=CampaignListResponse)
async def list_campaigns(
    reseller_id: Optional[str] = Query(None),
    merchant_id: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """List campaigns (newest first) with live lead aggregates."""
    return await list_campaigns_handler(
        current_user, reseller_id, merchant_id, page, limit
    )


@router.get("/campaigns/{campaign_id}", response_model=CampaignWithStats)
async def get_campaign(
    campaign_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """One campaign with live lead aggregates."""
    return await get_campaign_handler(campaign_id, current_user)


@router.post("/campaigns/{campaign_id}/stop", response_model=StopCampaignResponse)
async def stop_campaign(
    campaign_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Stop a running campaign and abort its pending leads."""
    return await stop_campaign_handler(campaign_id, current_user)
