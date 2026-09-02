"""The record module's doors: the per-customer journey view (A12), the event
ingest push door (A9), and the provider webhook bays (A9).

Thin routes per module rules §1. ``journey_router`` auths via
Depends(crm_admin_user) and delegates to timeline.py; ``ingest_router``
verifies the s2s caller and delegates to ingest.py — store first, 200
fast, understand later, and nothing here parses the payload;
``webhook_router`` (GET·POST /{provider}, mounted at /ingest/webhooks)
carries NO Depends at all — a provider cannot hold a bearer token, so each
request authenticates by its own ritual inside the registered bay
(ingress.py records the inversion that keeps rule 12 whole). Separate
routers, not one, because the doors mount under different prefixes with
different auth; the root router includes each exactly once.

Tenancy law holds on all: merchant_id is a required query param on the
journey read; on ingest the merchant in the envelope IS the merchant the
token was verified against; on a webhook the merchant is the ANSWER,
resolved per letter from the receiving endpoint by the bay.
"""

import json
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse

from app.core.logger import logger
from app.core.logger.context import set_log_context
from app.crm.auth import crm_admin_user, verify_s2s_caller
from app.crm.record import ingress
from app.crm.record.ingest import ingest_event
from app.crm.record.schemas import EventIn, EventReceipt, JourneyCard
from app.crm.record.timeline import get_customer_journey
from app.schemas import UserInfo

journey_router = APIRouter()
ingest_router = APIRouter()
webhook_router = APIRouter()


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


# --- the provider bays (design/ingest-doors) ----------------------------------
#
# One cap, one door: a letter is stored verbatim and forever whichever
# entrance it came through, so the bays share the envelope door's constant.
# Here the read ITSELF is bounded (streamed) rather than a declared
# Content-Length — a chunked body has no header to check, and these are the
# routes that buffer bytes from callers they have not authenticated yet.


@webhook_router.get("/{provider}", response_class=PlainTextResponse)
async def webhook_challenge_route(provider: str, request: Request) -> Response:
    """A provider's subscription challenge, echoed back once at registration.

    Meta calls this GET when the callback URL is saved in its dashboard.
    404, not 403, on refusal — and the same 404 for an unknown provider as
    for a wrong token: a different answer would tell an unauthenticated
    caller they found something worth guessing at.
    """
    set_log_context(component="crm.ingest.webhooks")
    spec = ingress.INGRESS.get(provider)
    challenge = spec.challenge(request.query_params) if spec else None
    if challenge is None:
        return PlainTextResponse("", status_code=status.HTTP_404_NOT_FOUND)
    logger.info(f"ingress: {provider} webhook subscription verified")
    # Echoed raw: Meta compares the body, so quoting or JSON-wrapping it
    # fails the handshake.
    return PlainTextResponse(challenge)


@webhook_router.post("/{provider}")
async def provider_webhook_route(provider: str, request: Request) -> Response:
    """Verify a provider callback and file its letters in the event spine.

    The raw bytes are read untouched — the bay's signature covers exactly
    what was sent, and parse-then-reserialise would break it forever. 200
    means RECEIVED, never UNDERSTOOD — but a STORE failure is 503, exactly
    as on the envelope door: the provider retries with the same ids and
    dedupe makes the retry safe, where a 200 on a dropped letter would lose
    it forever.
    """
    set_log_context(component="crm.ingest.webhooks")
    spec = ingress.INGRESS.get(provider)
    if spec is None:
        # No such bay — hidden exactly like a wrong handshake token, and
        # refused before a single body byte is read.
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_LETTER_BYTES:
            return Response(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    if not spec.verify(bytes(body), request.headers):
        # No detail: a caller who cannot sign has not earned an explanation.
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    try:
        parsed = json.loads(bytes(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = None
    if not isinstance(parsed, dict):
        # Signed by the provider, but not shaped like anything they
        # document. Worth a 400 and a log line rather than a silent 200.
        logger.error(f"ingress: a signed {provider} body was not a JSON object")
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    letters = await spec.envelope(request.headers, parsed)
    filed = 0
    for letter in letters:
        try:
            event_id = await ingest_event(
                merchant_id=letter.merchant_id,
                source=letter.source,
                topic=letter.topic,
                external_id=letter.external_id,
                payload=letter.payload,
                occurred_at=letter.occurred_at,
                schema_version=letter.schema_version,
            )
        except Exception as e:
            # Front door fails CLOSED, same as the push door above: a 200
            # here would silently drop the provider's letter; 503 tells
            # them to retry, and dedupe makes the retry safe.
            logger.error(
                f"ingress: store failed for {letter.source}/{letter.topic} "
                f"external_id={letter.external_id}: {e}"
            )
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        if event_id is not None:
            filed += 1
    logger.info(f"ingress: {provider} webhook filed {filed}/{len(letters)} letters")
    return Response(status_code=status.HTTP_200_OK)
