import json
from typing import Any, Dict, Literal
from uuid import uuid4

from fastapi import Request
from pydantic import BaseModel

from app.core.logger import logger
from app.services.redis import get_redis_service, is_redis_configured

_DEDUPE_TTL_SECONDS = 7 * 24 * 3600
_MAX_BODY_BYTES = 1024 * 1024


class WebhookAck(BaseModel):
    status: Literal["ignored", "success"]


async def _is_duplicate(event_key: str) -> bool:
    if not is_redis_configured():
        return False
    try:
        redis = await get_redis_service()
        client = await redis.get_client()
        was_set = await client.set(
            f"uap:webhook:{event_key}", "1", nx=True, ex=_DEDUPE_TTL_SECONDS
        )
    except Exception as e:
        logger.error(f"UAP webhook dedupe unavailable, processing anyway: {e}")
        return False
    return not was_set


async def handle_webhook(request: Request) -> WebhookAck:
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > _MAX_BODY_BYTES:
            logger.error(f"UAP webhook body exceeds {_MAX_BODY_BYTES} bytes")
            return WebhookAck(status="ignored")

    try:
        event = json.loads(raw)
    except ValueError:
        logger.error(f"UAP webhook non-JSON body ({len(raw)} bytes)")
        return WebhookAck(status="ignored")
    if not isinstance(event, dict):
        logger.error("UAP webhook body is not a JSON object")
        return WebhookAck(status="ignored")

    event_id = str(event.get("id") or "")
    event_name = str(event.get("event_name") or "")
    content = event.get("content")
    order = content.get("order") if isinstance(content, dict) else None
    if not isinstance(order, dict):
        order = {}
    order_id = str(order.get("order_id") or order.get("id") or "")

    dedupe_key = event_id or f"{order_id}:{event_name}:{order.get('status', '')}"
    if dedupe_key and await _is_duplicate(dedupe_key):
        logger.info(f">>> [webhook] duplicate skipped id={event_id} order={order_id}")
        return WebhookAck(status="success")

    logger.info(
        f">>> [webhook] event={event_name} order_id={order_id} "
        f"status={order.get('status')} id={event_id}"
    )
    logger.info(f">>> [webhook] full payload={event}")

    return WebhookAck(status="success")


async def handle_mock_txns(request: Request) -> Dict[str, Any]:
    """Stand-in for Juspay /txns: point a test credential's base_url at
    /uap/mock so create_txn round-trips without touching Juspay."""
    body = await request.json()
    order_id = str(body.get("order.order_id") or "")
    return {
        "order_id": order_id,
        "txn_id": f"{order_id}-1",
        "txn_uuid": f"mock-{uuid4().hex[:12]}",
        "status": "PENDING_VBV",
        "agentic_payments": {"sub_ref_id": f"mock-sub-{uuid4().hex[:8]}"},
    }


def mock_order_status_response(order_id: str) -> Dict[str, Any]:
    """Stand-in for Juspay GET /orders/{id}: the draw always settled."""
    return {
        "order_id": order_id,
        "status": "CHARGED",
        "amount": 27.0,
        "currency": "INR",
        "txn_uuid": f"mock-{order_id[-8:]}",
    }


def mock_booking_info_response(journey_id: str) -> Dict[str, Any]:
    """Stand-in for NY GET /v2/multimodal/{id}/booking/info AFTER payment:
    one booked Metro leg with tickets, matching the projection paths the
    chennai_one template reads (bookingStatus.contents, ticketNo, ...)."""
    return {
        "journeyId": journey_id,
        "journeyStatus": "CONFIRMED",
        "paymentOrderShortId": "mock-order-short-001",
        "unifiedQRV2": "mock-unified-qr-data",
        "legs": [
            {
                "journeyLegId": "mock-leg-001",
                "order": 1,
                "travelMode": "Metro",
                "bookingAllowed": True,
                "skipBooking": False,
                "bookingStatus": {"tag": "Booked", "contents": "Booked"},
                "totalFare": {"amount": 27.0, "currency": "INR"},
                "legExtraInfo": {
                    "tag": "Metro",
                    "contents": {
                        "providerName": "CMRL",
                        "bookingId": "frfs-mock-booking-001",
                        "ticketNo": ["TKT-2026-000123"],
                        "tickets": ["mock-qr-payload-001"],
                        "ticketValidity": ["2026-08-27T23:59:59Z"],
                        "routeInfo": [
                            {
                                "originStop": {"name": "Guindy Metro Station"},
                                "destinationStop": {
                                    "name": "Chennai Central Metro Station"
                                },
                                "routeCode": "CMRL-BLUE",
                                "platformNumber": "1",
                            }
                        ],
                    },
                },
            }
        ],
    }


def mock_initiate_response(journey_id: str) -> Dict[str, Any]:
    """Stand-in for NY initiate: point the ny_base template secret at
    /uap/mock so uap_pay's journey fetch round-trips offline too."""
    return {
        "journeyId": journey_id,
        "journeyStatus": "INITIATED",
        "estimatedMinFare": {"amount": 35.0, "currency": "INR"},
        "estimatedMaxFare": {"amount": 35.0, "currency": "INR"},
        "legs": [
            {
                "journeyLegId": "mock-leg-001",
                "order": 1,
                "travelMode": "Metro",
                "bookingAllowed": True,
                "skipBooking": False,
                "pricingId": "mock-pricing-001",
                "estimatedTotalFare": {"amount": 35.0, "currency": "INR"},
                "legExtraInfo": {
                    "tag": "Metro",
                    "contents": {
                        "providerName": "CMRL",
                        "routeInfo": [
                            {
                                "originStop": {"name": "Guindy Metro Station"},
                                "destinationStop": {
                                    "name": "Chennai Central Metro Station"
                                },
                                "routeCode": "CMRL-BLUE",
                            }
                        ],
                    },
                },
            }
        ],
    }
