"""Pydantic models the identity module exposes. LEAF — imports nothing
internal (the DTO->engine scar law): api.py, contracts signatures and
tests import shapes from here; only decoder.py translates rows into them."""

from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import BaseModel


class CrmCustomerSummary(BaseModel):
    """List-row shape: everything EXCEPT the attributes jsonb — the list
    endpoint neither fetches nor ships assertion history; the detail GET
    (CrmCustomer) carries it for ops debugging."""

    id: UUID
    merchant_id: str
    display_name: Optional[str] = None
    primary_locale: Optional[str] = None
    timezone: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    igsid: Optional[str] = None
    shopify_customer_id: Optional[str] = None
    external_ref: Optional[str] = None
    status: str
    merged_into_id: Optional[UUID] = None
    merged_at: Optional[datetime] = None
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime


class CrmCustomer(CrmCustomerSummary):
    """Full detail row: summary + the attributes assertion history."""

    attributes: Dict[str, Any] = {}
