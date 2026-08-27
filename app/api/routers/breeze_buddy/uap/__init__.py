from typing import Any, Dict

from fastapi import APIRouter, Request

from app.api.routers.breeze_buddy.uap import handlers
from app.api.routers.breeze_buddy.uap.handlers import WebhookAck

router = APIRouter()


@router.post("/webhook", response_model=WebhookAck)
async def uap_webhook(request: Request) -> WebhookAck:
    return await handlers.handle_webhook(request)


@router.post("/mock/txns")
async def uap_mock_txns(request: Request) -> Dict[str, Any]:
    return await handlers.handle_mock_txns(request)


@router.post("/mock/v2/multimodal/{journey_id}/initiate")
async def uap_mock_initiate(journey_id: str) -> Dict[str, Any]:
    return handlers.mock_initiate_response(journey_id)


@router.get("/mock/orders/{order_id}")
async def uap_mock_order_status(order_id: str) -> Dict[str, Any]:
    return handlers.mock_order_status_response(order_id)


@router.get("/mock/v2/multimodal/{journey_id}/booking/info")
async def uap_mock_booking_info(journey_id: str) -> Dict[str, Any]:
    return handlers.mock_booking_info_response(journey_id)
