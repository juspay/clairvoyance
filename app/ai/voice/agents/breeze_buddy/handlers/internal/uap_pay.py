"""Built-in global function: charge the customer's UPI agent via UAP /txns.

Exposed from templates as:

    {"type": "builtin", "name": "pay_with_upi_agent", "handler": "uap_pay",
     "description": "...", "properties": {"journey_id": {"type": "string"}},
     "required": ["journey_id"], "approval": {"prompt": "..."}}

The LLM passes only the journey_id. The handler re-fetches that journey
from NammaYatri (initiate) so the charged fare is authoritative at pay
time - the LLM never controls a money value. NY base URL + rider token
and the intent refs (``uap_action_id`` or ``uap_action_ref_id`` +
``uap_payer_avpa``) come from template vars/secrets. The canonical is
hardcoded around the fetched fare (see build_canonical in
app/services/uap/txns.py). Credentials resolve from the template's
reseller; point the ``uap`` credential's base_url and the ``ny_base``
secret at ``/agent/voice/breeze-buddy/uap/mock`` to run the whole loop
offline.
"""

import base64
import hashlib
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

import aiohttp

from app.core.config.static import JUSPAY_ACTION_ID, JUSPAY_MERCHANT_ID
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.services.uap.client import JuspayError
from app.services.uap.credentials import load_uap_credentials
from app.services.uap.txns import build_canonical, create_txn

_NY_TIMEOUT_SECONDS = 15


def _normalize_amount(value: Any) -> Optional[str]:
    """NY fares are numbers (40.0); the wire wants a two-decimal string."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    except InvalidOperation:
        return None


async def _fetch_journey(
    ny_base: str, rider_token: str, journey_id: str
) -> Dict[str, Any]:
    """POST /v2/multimodal/{journey_id}/initiate - exact fares for one journey."""
    base = ny_base.rstrip("/").removesuffix("/v2")
    url = f"{base}/v2/multimodal/{journey_id}/initiate"
    logger.info(f">>> [uap_pay] NY fetch POST {url}")
    async with create_aiohttp_session() as session:
        async with session.post(
            url,
            json={},
            headers={"token": rider_token, "Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=_NY_TIMEOUT_SECONDS),
        ) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(f"NY initiate HTTP {response.status}: {body}")
            return body


async def _confirm_journey(
    ny_base: str, rider_token: str, journey_id: str, journey: Dict[str, Any]
) -> Dict[str, Any]:
    """POST /v2/multimodal/{journey_id}/confirm - tell NY to book the tickets.

    One element per leg: skipBooking for non-bookable (walk) legs, one adult
    ticket on each bookable leg. NY responds with orderSdkPayload carrying
    ITS payment order (orderId + final amount) - the order that must turn
    PAID for tickets to be issued.
    """
    elements = []
    for leg in journey.get("legs") or []:
        bookable = bool(leg.get("bookingAllowed"))
        elements.append(
            {
                "journeyLegOrder": leg.get("order"),
                "skipBooking": not bookable,
                "ticketQuantity": 1 if bookable else None,
                "childTicketQuantity": 0 if bookable else None,
                "categorySelectionReq": None,
                "crisSdkResponse": None,
                "vehicleNumber": None,
                "tripId": None,
                "seatIds": None,
            }
        )
    body = {"enableOffer": None, "journeyConfirmReqElements": elements}
    base = ny_base.rstrip("/").removesuffix("/v2")
    url = f"{base}/v2/multimodal/{journey_id}/confirm"
    logger.info(f">>>>> [uap_pay] NY confirm POST {url} body={body}")
    async with create_aiohttp_session() as session:
        async with session.post(
            url,
            json=body,
            headers={"token": rider_token, "Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=_NY_TIMEOUT_SECONDS),
        ) as response:
            payload = await response.json(content_type=None)
            logger.info(
                f">>>>> [uap_pay] NY confirm -> HTTP {response.status} body={payload}"
            )
            if response.status >= 400:
                raise RuntimeError(f"NY confirm HTTP {response.status}: {payload}")
            return payload


def _canonical_fields(journey: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, str]]:
    """Amount + canonical overrides off the raw initiate response.

    Returns (None, {}) while any bookable leg is still unpriced - the
    draw must not fire on a journey NY hasn't finished pricing.
    """
    legs = [leg for leg in journey.get("legs") or [] if leg.get("bookingAllowed")]
    if not legs or any(not leg.get("pricingId") for leg in legs):
        return None, {}

    amount = _normalize_amount((journey.get("estimatedMaxFare") or {}).get("amount"))
    leg = legs[0]
    extra = (leg.get("legExtraInfo") or {}).get("contents") or {}
    route = (extra.get("routeInfo") or [{}])[0]
    origin = (route.get("originStop") or {}).get("name") or "?"
    destination = (route.get("destinationStop") or {}).get("name") or "?"
    line = route.get("routeCode") or "?"
    overrides = {
        "sku": leg.get("pricingId") or "pricing-unknown",
        "name": f"{leg.get('travelMode', 'Ticket')} {origin}-{destination} ({line}) - ADULT"[
            :128
        ],
        "journey_id": str(journey.get("journeyId") or ""),
        "leg_id": str(leg.get("journeyLegId") or ""),
        "provider": extra.get("providerName") or "?",
        "order_ref": str(journey.get("journeyId") or ""),
    }
    return amount, overrides


async def uap_pay(context: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f">>> [uap_pay] step 1/6 START args={args}")
    template = getattr(context.bot, "template", None)
    reseller_id = getattr(template, "reseller_id", None)
    if not reseller_id:
        logger.error(">>> [uap_pay] ABORT: template has no reseller_id")
        return {"status": "error", "error": "template has no reseller_id"}

    journey_id = args.get("journey_id")
    if not isinstance(journey_id, str) or not journey_id:
        logger.error(">>> [uap_pay] ABORT: journey_id missing from LLM args")
        return {"status": "error", "error": "journey_id is required"}

    template_vars = getattr(context.bot, "template_vars", None) or {}
    session_state = getattr(context.bot, "agent_state", None) or {}

    def resolve(key: str):
        value = session_state.get(key) or template_vars.get(key)
        if not value and key == "uap_action_id":
            value = JUSPAY_ACTION_ID
        if not value and key == "uap_merchant_id":
            value = JUSPAY_MERCHANT_ID
        return value

    ny_base = template_vars.get("ny_base")
    rider_token = template_vars.get("rider_token")
    if not ny_base or not rider_token:
        logger.error(">>> [uap_pay] ABORT: ny_base / rider_token missing from secrets")
        return {
            "status": "error",
            "error": "ny_base / rider_token missing from template secrets",
        }
    logger.info(
        f">>> [uap_pay] step 2/6 inputs OK reseller={reseller_id} "
        f"action_id={resolve('uap_action_id')} "
        f"journey_id={journey_id} ny_base={ny_base}"
    )

    try:
        journey = await _fetch_journey(ny_base, rider_token, journey_id)
    except Exception as e:
        logger.error(
            f">>> [uap_pay] ABORT: journey fetch failed journey_id={journey_id}: {e}"
        )
        return {"status": "error", "error": f"could not load journey: {e}"}
    logger.info(
        f">>> [uap_pay] step 3/6 NY initiate fetched status={journey.get('journeyStatus')} "
        f"maxFare={journey.get('estimatedMaxFare')} legs={len(journey.get('legs') or [])}"
    )

    amount, overrides = _canonical_fields(journey)
    if amount is None:
        logger.error(">>> [uap_pay] ABORT: bookable legs unpriced (pricingId null)")
        return {
            "status": "error",
            "error": "journey has no priced bookable legs yet - fares still loading",
        }
    logger.info(
        f">>> [uap_pay] step 4/6 amount={amount} canonical_overrides={overrides}"
    )

    try:
        confirm = await _confirm_journey(ny_base, rider_token, journey_id, journey)
    except Exception as e:
        logger.error(
            f">>>>> [uap_pay] ABORT: NY confirm failed journey_id={journey_id}: {e}"
        )
        return {"status": "error", "error": f"could not confirm booking: {e}"}
    if str(confirm.get("result") or "").upper() == "FAILED":
        logger.error(f">>>>> [uap_pay] ABORT: NY confirm result=FAILED {confirm}")
        return {"status": "error", "error": "NY could not confirm the booking"}

    sdk_payload = confirm.get("orderSdkPayload") or {}
    ny_order_id = sdk_payload.get("order_id") or sdk_payload.get("orderId")
    inner = sdk_payload.get("sdk_payload") or sdk_payload.get("sdkPayload") or {}
    ny_amount = _normalize_amount((inner.get("payload") or {}).get("amount"))
    if ny_amount is not None:
        amount = ny_amount
    logger.info(
        f">>>>> [uap_pay] NY confirmed: ny_order_id={ny_order_id} "
        f"ny_amount={ny_amount} charge_amount={amount} "
        f"gateway_ref={confirm.get('gatewayReferenceId')}"
    )

    order_id = f"uap_{uuid4().hex[:11]}"
    canonical = build_canonical(amount, **overrides)
    logger.info(f">>> [uap_pay] canonical={canonical}")
    try:
        creds = await load_uap_credentials(reseller_id)
        logger.info(
            f">>> [uap_pay] step 5/6 credentials loaded merchant_id={creds.merchant_id} "
            f"base_url={creds.base_url} api_key=***{creds.api_key[-4:]} "
            f"-> firing /txns order_id={order_id}"
        )
        response = await create_txn(
            order_id=order_id,
            amount=amount,
            items_canonical=canonical,
            customer_id=resolve("uap_customer_id"),
            action_id=resolve("uap_action_id"),
            action_ref_id=resolve("uap_action_ref_id"),
            payer_avpa=resolve("uap_payer_avpa"),
            gateway_id=template_vars.get("uap_gateway_id"),
            gateway_reference_id=template_vars.get("uap_gateway_reference_id"),
            x_feature=template_vars.get("uap_x_feature"),
            euler_api_gateway=template_vars.get("uap_euler_api_gateway"),
            service_type=template_vars.get("uap_service_type"),
            user_prompt_hash="sha256:"
            + base64.urlsafe_b64encode(
                hashlib.sha256(f"book journey {journey_id}".encode()).digest()
            )
            .decode()
            .rstrip("="),
            proposed_expiry=str(int(time.time()) + 900),
            api_key=creds.api_key,
            merchant_id=resolve("uap_merchant_id") or creds.merchant_id,
            base_url=creds.base_url,
        )
    except (ValueError, JuspayError) as e:
        logger.error(f">>> [uap_pay] ABORT: /txns failed order_id={order_id}: {e}")
        return {"status": "error", "error": str(e)}

    result = {
        "status": "success",
        "order_id": str(response.get("order_id") or order_id),
        "amount": amount,
        "journey_id": journey_id,
        "ny_order_id": ny_order_id,
        "txn_status": str(response.get("status") or "UNKNOWN"),
        "txn_uuid": response.get("txn_uuid"),
        "sub_ref_id": (response.get("agentic_payments") or {}).get("sub_ref_id"),
    }
    logger.info(f">>> [uap_pay] step 6/6 DONE {result}")
    return result
