"""The record module's two doors (/customers and /ingest): the per-customer journey view (A12) and the
event ingest push door (A9).

Thin routes per module rules §1. ``journey_router`` auths via
Depends(crm_admin_user) and delegates to timeline.py; ``ingest_router``
verifies the s2s caller and delegates to ingest.py — store first, 200
fast, understand later, and nothing here parses the payload. Two routers,
not one, because the two doors mount under different prefixes with
different auth; the root router includes each exactly once.

Tenancy law holds on both: merchant_id is a required query param on the
journey read, and on ingest the merchant in the envelope IS the merchant
the token was verified against.
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.core.logger import logger
from app.core.logger.context import set_log_context
from app.crm.auth import crm_admin_user, verify_s2s_caller
from app.crm.record.ingest import ingest_event
from app.crm.record.schemas import EventIn, EventReceipt, JourneyCard
from app.crm.record.timeline import get_customer_journey
from app.schemas import UserInfo

journey_router = APIRouter()
ingest_router = APIRouter()


@journey_router.get("/{customer_id}/journey", response_model=List[JourneyCard])
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


# A letter is stored verbatim and forever, so "store first" must not mean
# "store anything of any size". Nothing in front of this app caps a request
# body today (no ingress rule, no uvicorn flag), so the door does it.
MAX_LETTER_BYTES = 1 * 1024 * 1024


async def within_size_limit(request: Request) -> None:
    """413 before the row exists. Declared beside the auth dependency for
    the same reason: a door's limits belong on the door, not in a handler
    that the next route beside it might forget to copy."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_LETTER_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Event exceeds {MAX_LETTER_BYTES} bytes",
        )


async def verified_caller(event: EventIn, request: Request) -> str:
    """The ingest door's auth, as the route's declared dependency.

    A dependency rather than a first line in the handler, because a route
    without its auth dependency is a BLOCKER (design/ingest-doors): the
    declaration is what makes the check impossible to forget when the next
    route lands beside this one, and FastAPI runs it before the handler
    body exists — so auth-before-store is the framework's guarantee, not a
    convention this file has to keep.

    It takes the parsed envelope because the merchant being claimed is IN
    the body; FastAPI parses it once and hands the same object to the
    handler.
    """
    return await verify_s2s_caller(event.merchant_id, request)


@ingest_router.post("/events", response_model=EventReceipt)
async def push_event_route(
    event: EventIn,
    _caller: str = Depends(verified_caller),
    _size: None = Depends(within_size_limit),
) -> EventReceipt:
    set_log_context(component="crm.ingest.push", merchant_id=event.merchant_id)
    try:
        event_id = await ingest_event(
            merchant_id=event.merchant_id,
            source=event.source,
            topic=event.topic,
            external_id=event.external_id,
            payload=event.payload,
            occurred_at=event.occurred_at,
            schema_version=event.schema_version,
        )
    except Exception as e:
        # Front door fails CLOSED: a 200 here would silently drop the
        # producer's event; 503 tells them to retry (dedupe makes the
        # retry safe).
        logger.error(
            f"push door store failed for {event.source}/{event.topic} "
            f"external_id={event.external_id}: {e}"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Event store unavailable — retry with the same external_id",
        )
    return EventReceipt(id=event_id, duplicate=event_id is None)
