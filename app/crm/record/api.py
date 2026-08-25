"""/crm/customers/{customer_id}/journey — the per-customer timeline (A12).

Thin route per module rules §1: auth via Depends, delegate straight to
timeline.py. Tenancy law: merchant_id is a required query param.
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from app.core.logger.context import set_log_context
from app.crm.auth import crm_admin_user
from app.crm.record.schemas import JourneyCard
from app.crm.record.timeline import get_customer_journey
from app.schemas import UserInfo

router = APIRouter()


@router.get("/{customer_id}/journey", response_model=List[JourneyCard])
async def get_customer_journey_route(
    customer_id: str,
    merchant_id: str = Query(..., description="Tenant scope — required"),
    limit: int = Query(50, ge=1, le=200),
    before_started_at: Optional[datetime] = Query(
        None, description="Keyset cursor: started_at of the last row seen"
    ),
    before_id: Optional[str] = Query(
        None, description="Keyset cursor: id of the last row seen"
    ),
    current_user: UserInfo = Depends(crm_admin_user),
) -> List[JourneyCard]:
    set_log_context(component="crm.record.journey", merchant_id=merchant_id)
    return await get_customer_journey(
        merchant_id, customer_id, limit, before_started_at, before_id
    )
