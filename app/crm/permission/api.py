"""/crm/consent — the console's consent capture (B1).

Thin route per module rules §1: auth via Depends, delegate to the logic file.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.logger.context import set_log_context
from app.crm.auth import crm_admin_user
from app.crm.permission.consent import CustomerNotInMerchant, record_consent
from app.crm.permission.schemas import ConsentEventIn, ConsentReceipt
from app.schemas import UserInfo

router = APIRouter()


@router.post("", response_model=ConsentReceipt, status_code=status.HTTP_201_CREATED)
async def record_consent_route(
    event: ConsentEventIn,
    current_user: UserInfo = Depends(crm_admin_user),
) -> ConsentReceipt:
    """``states`` is empty when the stored answer refuses the event; the 201
    still holds, because the attempt is in the ledger."""
    set_log_context(component="crm.consent.record", merchant_id=event.merchant_id)
    try:
        return await record_consent(event)
    except CustomerNotInMerchant as e:
        # 404 on the pair, not 403: separating "wrong merchant" from "no such
        # customer" would let a caller probe other tenants' ids.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
