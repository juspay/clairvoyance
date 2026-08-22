"""/crm/customers — the admin endpoints the console list + 360 read (A6).

Thin routes per module rules §1: auth via Depends, delegate to the
accessor. Tenancy law: every query carries its merchant_id predicate.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.logger.context import set_log_context
from app.crm.auth import crm_admin_user
from app.crm.identity.db.accessor import get_customer, list_customers
from app.crm.identity.schemas import CrmCustomer, CrmCustomerSummary
from app.schemas import UserInfo

router = APIRouter()


@router.get("", response_model=List[CrmCustomerSummary])
async def list_customers_route(
    merchant_id: str = Query(..., description="Tenant scope — required"),
    q: Optional[str] = Query(None, description="Phone/email (any format) or name"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: UserInfo = Depends(crm_admin_user),
) -> List[CrmCustomerSummary]:
    set_log_context(component="crm.customers.list", merchant_id=merchant_id)
    return await list_customers(merchant_id, q, limit, offset)


@router.get("/{customer_id}", response_model=CrmCustomer)
async def get_customer_route(
    customer_id: str,
    merchant_id: str = Query(..., description="Tenant scope — required"),
    current_user: UserInfo = Depends(crm_admin_user),
) -> CrmCustomer:
    set_log_context(component="crm.customers.get", merchant_id=merchant_id)
    customer = await get_customer(merchant_id, customer_id)
    if customer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found"
        )
    return customer
