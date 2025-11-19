from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from app.schemas.breeze_buddy.core import ExecutionMode


class OrderItem(BaseModel):
    product_name: str
    quantity: int


class OrderData(BaseModel):
    items: List[OrderItem]


class PushLeadRequest(BaseModel):
    request_id: str
    payload: Dict[str, Any]
    template: str
    merchant: str
    identifier: Optional[str] = None
    reporting_webhook_url: str | None = None
    execution_mode: Optional[ExecutionMode] = (
        None  # Defaults to TELEPHONY if not provided
    )


class LeadData(BaseModel):
    customer_mobile_number: str
    shop_identifier: Optional[str] = None
    shop_name: str
    order_data: OrderData
    total_price: float
    customer_name: str
    customer_address: str
    order_id: str
    identity: str | None = None
    reporting_webhook_url: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class LeadCancellation(BaseModel):
    lead_id: str
    cancellation_reason: str


class CancelLeadRequest(BaseModel):
    leads: List[LeadCancellation]
