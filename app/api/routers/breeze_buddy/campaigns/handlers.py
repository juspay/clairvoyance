"""Campaign handlers.

A campaign is a named bulk lead push: every row goes through the SAME
``push_lead_handler`` used by POST /leads (template ownership check, chat-agent
gate, blacklist, payload-schema validation, config lookup, dispatch
scheduling), then gets stamped with the campaign id. Rejected rows are
reported per-row instead of failing the batch.
"""

import json
from typing import List, Optional
from uuid import uuid4

from fastapi import HTTPException, status

from app.ai.voice.agents.breeze_buddy.dispatch import cancel_scheduled_lead
from app.ai.voice.agents.breeze_buddy.types.models import PushLeadRequest
from app.api.routers.breeze_buddy.leads.handlers import push_lead_handler
from app.core.logger import logger
from app.database.accessor import handle_lead_abort
from app.database.accessor.breeze_buddy.campaigns import (
    create_campaign,
    get_campaign_by_id,
    get_campaign_pending_lead_ids,
    get_campaign_stats,
    list_campaigns,
    stamp_lead_campaign,
    update_campaign_status,
    update_campaign_totals,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.campaigns import (
    Campaign,
    CampaignLeadError,
    CampaignListResponse,
    CampaignStats,
    CampaignStatus,
    CampaignWithStats,
    CreateCampaignRequest,
    CreateCampaignResponse,
    StopCampaignResponse,
)


def _is_admin(user: UserInfo) -> bool:
    return user.role == "admin" or "*" in (user.reseller_ids or [])


def _require_scope_access(
    user: UserInfo, reseller_id: str, merchant_id: Optional[str]
) -> None:
    if _is_admin(user):
        return
    if reseller_id in (user.reseller_ids or []):
        return
    if merchant_id and (
        merchant_id in (user.merchant_ids or []) or "*" in (user.merchant_ids or [])
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have access to this campaign scope",
    )


async def _maybe_complete(campaign: Campaign, stats: CampaignStats) -> Campaign:
    """Lazily flip RUNNING -> COMPLETED once nothing is pending."""
    if (
        campaign.status == CampaignStatus.RUNNING
        and stats.total > 0
        and stats.backlog == 0
        and stats.processing == 0
    ):
        updated = await update_campaign_status(
            campaign.id, CampaignStatus.COMPLETED.value
        )
        return updated or campaign
    return campaign


async def create_campaign_handler(
    req: CreateCampaignRequest, current_user: UserInfo
) -> CreateCampaignResponse:
    _require_scope_access(current_user, req.reseller_id, req.merchant_id)

    campaign = await create_campaign(
        reseller_id=req.reseller_id,
        merchant_id=req.merchant_id,
        template_id=req.template_id,
        name=req.name.strip(),
        created_by=current_user.username,
    )
    if not campaign:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create campaign",
        )

    queued = 0
    skipped: List[CampaignLeadError] = []
    for i, lead in enumerate(req.leads):
        push_req = PushLeadRequest(
            request_id=lead.request_id
            or f"cmp-{campaign.id[:8]}-{i + 1}-{uuid4().hex[:6]}",
            payload=lead.payload,
            template_id=req.template_id,
            reseller_id=req.reseller_id,
            merchant_id=req.merchant_id,
        )
        try:
            result = await push_lead_handler(push_req, current_user)
            lead_id = result.get("lead_call_tracker_id")
            if lead_id:
                await stamp_lead_campaign(str(lead_id), campaign.id)
                queued += 1
            else:
                skipped.append(
                    CampaignLeadError(row=i + 1, reason="lead was not created")
                )
        except HTTPException as e:
            skipped.append(CampaignLeadError(row=i + 1, reason=str(e.detail)))
        except Exception as e:  # keep the batch going; report the row
            logger.error(
                f"Campaign {campaign.id} row {i + 1} failed: {e}", exc_info=True
            )
            skipped.append(CampaignLeadError(row=i + 1, reason="internal error"))

    campaign = (
        await update_campaign_totals(
            campaign.id,
            queued,
            json.dumps([s.model_dump() for s in skipped]),
        )
        or campaign
    )
    if queued == 0:
        # Nothing made it in — an all-rejected batch is STOPPED, not RUNNING.
        campaign = (
            await update_campaign_status(campaign.id, CampaignStatus.STOPPED.value)
            or campaign
        )

    logger.info(
        f"Campaign '{campaign.name}' ({campaign.id}) created by "
        f"{current_user.username}: {queued} queued, {len(skipped)} skipped"
    )
    return CreateCampaignResponse(campaign=campaign, queued=queued, skipped=skipped)


async def list_campaigns_handler(
    current_user: UserInfo,
    reseller_id: Optional[str],
    merchant_id: Optional[str],
    page: int,
    limit: int,
) -> CampaignListResponse:
    reseller_ids: Optional[List[str]] = [reseller_id] if reseller_id else None
    merchant_ids: Optional[List[str]] = [merchant_id] if merchant_id else None
    if not _is_admin(current_user):
        # Narrow to the token's scope; explicit params can only narrow further.
        token_resellers = [r for r in (current_user.reseller_ids or []) if r != "*"]
        token_merchants = [m for m in (current_user.merchant_ids or []) if m != "*"]
        if reseller_id and token_resellers and reseller_id not in token_resellers:
            raise HTTPException(status_code=403, detail="reseller_id out of scope")
        if merchant_id and token_merchants and merchant_id not in token_merchants:
            raise HTTPException(status_code=403, detail="merchant_id out of scope")
        if not reseller_ids and token_resellers:
            reseller_ids = token_resellers
        if not merchant_ids and token_merchants:
            merchant_ids = token_merchants

    campaigns, total = await list_campaigns(reseller_ids, merchant_ids, page, limit)
    stats = await get_campaign_stats([c.id for c in campaigns])
    out: List[CampaignWithStats] = []
    for c in campaigns:
        s = stats.get(c.id, CampaignStats())
        c = await _maybe_complete(c, s)
        out.append(CampaignWithStats(campaign=c, stats=s))
    return CampaignListResponse(campaigns=out, total=total, page=page, limit=limit)


async def get_campaign_handler(
    campaign_id: str, current_user: UserInfo
) -> CampaignWithStats:
    campaign = await get_campaign_by_id(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    _require_scope_access(current_user, campaign.reseller_id, campaign.merchant_id)
    stats = (await get_campaign_stats([campaign.id])).get(campaign.id, CampaignStats())
    campaign = await _maybe_complete(campaign, stats)
    return CampaignWithStats(campaign=campaign, stats=stats)


async def stop_campaign_handler(
    campaign_id: str, current_user: UserInfo
) -> StopCampaignResponse:
    campaign = await get_campaign_by_id(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    _require_scope_access(current_user, campaign.reseller_id, campaign.merchant_id)
    if campaign.status != CampaignStatus.RUNNING:
        raise HTTPException(
            status_code=409, detail=f"Campaign is already {campaign.status.value}"
        )

    pending = await get_campaign_pending_lead_ids(campaign_id)
    aborted = 0
    for lead_id in pending:
        try:
            result = await handle_lead_abort(
                lead_id, f"campaign stopped by {current_user.username}"
            )
            if result:
                aborted += 1
                await cancel_scheduled_lead(lead_id)
        except Exception as e:
            logger.error(
                f"Failed to abort lead {lead_id} for campaign {campaign_id}: {e}"
            )

    updated = await update_campaign_status(campaign_id, CampaignStatus.STOPPED.value)
    logger.info(
        f"Campaign {campaign_id} stopped by {current_user.username}; "
        f"{aborted}/{len(pending)} pending leads aborted"
    )
    return StopCampaignResponse(campaign=updated or campaign, aborted_leads=aborted)
