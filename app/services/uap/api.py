"""Outbound API calls for agentic payments: Juspay (customers, AOP agents and
actions, /txns) and NammaYatri (initiate / confirm / booking info).
Transport + credentials, Juspay customers / AOP / txns, NammaYatri journeys, and the
onboarding expiry watcher. Section markers name the file each block came from."""

import asyncio
import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp

from app.core.config.static import (
    EULER_BASE_URL,
    EULER_GATEWAY_ID,
    EULER_TIMEOUT_SECONDS,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.crm.agentic.contracts import (
    TERMINAL_STATUSES,
    CrmCustomerAgent,
    apply_juspay_records,
    get_by_agent_obj_ref,
    pick_action,
)
from app.database.accessor.breeze_buddy.credentials import get_credentials_by_merchant
from app.services.redis import get_redis_service, is_redis_configured


# ---- transport + credentials ----
class JuspayError(RuntimeError):
    def __init__(self, message: str, status: Optional[int], body: Any):
        super().__init__(message)
        self.status = status
        self.body = body


# Never logged in clear: a live credential, or the rider's contact details.
_REDACT_KEYS = {
    "client_auth_token",
    "mobile_number",
    "email_address",
    "customer_phone",
    "customer_email",
    "order.customer_phone",
    "order.customer_email",
}


def _redact(value: Any) -> Any:
    """A copy of ``value`` with secret / personal fields masked, for logs."""
    if isinstance(value, dict):
        return {
            k: ("***" if k in _REDACT_KEYS and v not in (None, "") else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _form_fields(data: Dict[str, Any]) -> Dict[str, str]:
    """Flatten a payload for application/x-www-form-urlencoded.

    str(True) is "True", which Juspay rejects, and nested values travel as
    JSON strings inside a single field. None means "not sent".
    """
    fields: Dict[str, str] = {}
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, bool):
            fields[key] = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            fields[key] = json.dumps(value)
        else:
            fields[key] = str(value)
    return fields


async def request(
    method: str,
    path: str,
    *,
    api_key: Optional[str],
    merchant_id: Optional[str],
    base_url: Optional[str],
    json_body: Optional[Dict[str, Any]] = None,
    form_body: Optional[Dict[str, Any]] = None,
    routing_id: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    if not api_key:
        raise ValueError("api_key is required")
    if not merchant_id:
        raise ValueError("merchant_id is required")
    # GET carries no body; the AOP status/list reads use it.
    if method.upper() != "GET" and json_body is None and form_body is None:
        raise ValueError("one of json_body or form_body is required")
    base_url = (base_url or EULER_BASE_URL).rstrip("/")

    # Keys are stored RAW; the encoding happens here and only here. A value
    # that already carries the scheme is a misconfigured credential row.
    if api_key.startswith("Basic "):
        raise ValueError("api_key must be the raw Juspay key, not a 'Basic …' header")
    authorization = "Basic " + base64.b64encode(f"{api_key}:".encode()).decode()
    headers = {"Authorization": authorization}
    if extra_headers:
        headers.update(extra_headers)
    if routing_id:
        # Juspay requires this to stay constant for every request tied to
        # one customer.
        headers["x-routing-id"] = routing_id

    kwargs: Dict[str, Any] = {}
    if form_body is not None:
        kwargs["data"] = _form_fields(form_body)
    elif json_body is not None:
        kwargs["json"] = json_body

    logger.info(f"uap juspay: {method} {path} merchant_id={merchant_id}")
    logger.debug(
        f"uap juspay: {method} {path} routing_id={routing_id} "
        f"body={_redact(kwargs.get('json') or kwargs.get('data'))}"
    )
    async with create_aiohttp_session() as session:
        async with session.request(
            method,
            f"{base_url}{path}",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=EULER_TIMEOUT_SECONDS),
            **kwargs,
        ) as response:
            text = await response.text()
            logger.info(f"uap juspay: {method} {path} -> HTTP {response.status}")
            try:
                decoded = json.loads(text)
                logger.debug(f"uap juspay: {method} {path} body={_redact(decoded)}")
            except ValueError:
                logger.error(
                    f"uap juspay: {path} non-JSON body (HTTP {response.status})"
                )
                raise JuspayError(
                    f"Juspay {path} returned a non-JSON body (HTTP {response.status})",
                    response.status,
                    text,
                )
            if response.status >= 400:
                logger.error(
                    f"uap juspay: {path} failed HTTP {response.status}: {decoded}"
                )
                raise JuspayError(
                    f"Juspay {path} failed with HTTP {response.status}",
                    response.status,
                    decoded,
                )
            return decoded


# ---- credentials ----


CREDENTIAL_NAME = "uap"


@dataclass(frozen=True)
class JuspayCredentials:
    """The reseller's ``uap`` credential row. The Juspay HOST is not in it:
    one service talks to one Juspay environment (``UAP_ENVIRONMENT``), so a
    per-row ``base_url`` could only ever disagree with it."""

    api_key: str
    merchant_id: str
    # AOP (agentic) APIs — /v1/aop/* — are keyed by the partner's key, not
    # the merchant's; a merchant key answers them with HTTP 500. Falls back
    # to api_key so a single-key setup still works.
    aop_api_key: Optional[str] = None
    # Shared secret Juspay must echo on webhook calls (?token=…). Generated
    # per reseller; a webhook without it is dropped before it is parsed.
    webhook_token: Optional[str] = None
    # Euler gateway for agentic /txns; unset = EULER_GATEWAY_ID.
    gateway_id: Optional[str] = None
    # The merchant's own NammaYatri backend (journey confirm, payment
    # status, booking info). Per reseller like the rest of this row — it is
    # THE place the backend learns which NY host to call; never the request.
    ny_base_url: Optional[str] = None

    @property
    def aop_key(self) -> str:
        return self.aop_api_key or self.api_key

    @property
    def base_url(self) -> str:
        return EULER_BASE_URL

    @property
    def txns_base(self) -> str:
        return EULER_BASE_URL


def pick_uap_row(rows: List[Any], name: str, merchant_id: Optional[str]) -> Any:
    """Most specific active row named ``name``: the merchant's own, else the
    reseller-wide one, else the global one. Pure — unit-tested."""
    best, best_rank = None, -1
    for row in rows:
        if row.name != name or not row.is_active:
            continue
        row_merchant = getattr(row, "merchant_id", None)
        if row_merchant and row_merchant != merchant_id:
            continue
        rank = 2 if row_merchant else (1 if row.reseller_id else 0)
        if rank > best_rank:
            best, best_rank = row, rank
    return best


async def load_uap_credentials(
    reseller_id: str, merchant_id: Optional[str] = None, name: str = CREDENTIAL_NAME
) -> JuspayCredentials:
    """Load the tenant's UAP credential: the merchant's own row when it has
    one, else the reseller's, else the global row.

    The lookup returns every credential this tenant can see, so ``name``
    selects ours and ``pick_uap_row`` applies most-specific-wins.
    """
    rows = await get_credentials_by_merchant(
        reseller_id, mask=False, merchant_id=merchant_id
    )
    match = pick_uap_row(rows, name, merchant_id)
    if match is None:
        raise JuspayError(
            f"No active '{name}' credential for {reseller_id}"
            + (f"/{merchant_id}" if merchant_id else ""),
            None,
            None,
        )

    value = match.value or {}
    api_key = value.get("api_key")
    merchant_id = value.get("merchant_id")
    if not api_key or not merchant_id:
        raise JuspayError(
            f"'{name}' credential unusable: missing api_key or merchant_id "
            f"(or failed to decrypt)",
            None,
            None,
        )

    return JuspayCredentials(
        api_key=api_key,
        merchant_id=merchant_id,
        aop_api_key=value.get("aop_api_key"),
        webhook_token=value.get("webhook_token"),
        gateway_id=value.get("gateway_id") or None,
        ny_base_url=(value.get("ny_base_url") or "").rstrip("/") or None,
    )


# ---- Juspay customers ----


async def create_or_get_customer(
    creds: JuspayCredentials,
    object_reference_id: str,
    mobile_number: str,
    mobile_country_code: str = "91",
    email_address: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Create the customer if absent, else return the existing one.

    ``object_reference_id`` is our own id for the user: >= 8 chars, and it
    must never change — a new value mints a new customer and orphans the
    user's existing agent and mandates.

    Returns the raw response. ``id`` is the ``cst_…`` to persist;
    ``juspay.client_auth_token`` expires in 15 minutes, so pass it straight
    to the SDK and never store it.
    """
    if len(object_reference_id) < 8:
        raise ValueError("object_reference_id must be at least 8 characters")
    if not mobile_number:
        raise ValueError("mobile_number is required")

    response = await request(
        "POST",
        f"/v2/customers/{quote(object_reference_id, safe='')}",
        api_key=creds.api_key,
        merchant_id=creds.merchant_id,
        base_url=creds.base_url,
        form_body={
            "object_reference_id": object_reference_id,
            "mobile_number": mobile_number,
            "mobile_country_code": mobile_country_code,
            "email_address": email_address,
            "first_name": first_name,
            "last_name": last_name,
            "options.get_client_auth_token": True,
        },
        routing_id=object_reference_id,
    )

    if not (response.get("juspay") or {}).get("client_auth_token"):
        raise JuspayError("Juspay returned no client_auth_token", None, response)
    return response


# ---- Juspay AOP agents / actions ----


# Juspay answers a lookup for an agent whose onboarding lapsed with an
# error body rather than a status field; the message is the only signal.
_EXPIRED_MARKER = "onboarding expired"


async def _get(creds: JuspayCredentials, path: str) -> Dict[str, Any]:
    return await request(
        "GET",
        path,
        api_key=creds.aop_key,
        merchant_id=creds.merchant_id,
        base_url=creds.base_url,
    )


async def get_agent_by_ref(
    creds: JuspayCredentials, agent_obj_ref: str
) -> Optional[Dict[str, Any]]:
    """The agent we asked the SDK to create, by our own reference.

    None when Juspay reports the onboarding expired — the rider never
    finished consent and the reference is dead; a new attempt needs a new
    reference. Any other failure raises, so a network blip is never read
    as "expired".
    """
    try:
        return await _get(
            creds, f"/v1/aop/agent?object_reference_id={quote(agent_obj_ref, safe='')}"
        )
    except JuspayError as exc:
        if (
            _EXPIRED_MARKER in str(exc).lower()
            or _EXPIRED_MARKER in json.dumps(getattr(exc, "body", None) or {}).lower()
        ):
            return None
        raise


async def list_agents(
    creds: JuspayCredentials, juspay_customer_id: str
) -> List[Dict[str, Any]]:
    """Every agent Juspay holds for this customer — the check before a new
    onboarding, so a rider who already consented is never asked twice."""
    body = await _get(
        creds, f"/v1/aop/agents?customer_id={quote(juspay_customer_id, safe='')}"
    )
    agents = body.get("agents") if isinstance(body, dict) else None
    return [a for a in (agents or []) if isinstance(a, dict)]


async def get_action(creds: JuspayCredentials, action_id: str) -> Dict[str, Any]:
    return await _get(creds, f"/v1/aop/action/{quote(action_id, safe='')}")


# ---- refresh: agent + action -> row ----


async def refresh_agent(
    creds: JuspayCredentials, row: CrmCustomerAgent
) -> Optional[CrmCustomerAgent]:
    """Re-read from Juspay and apply. Returns the updated row, or the row
    unchanged when Juspay cannot be reached (callers decide the posture:
    a draw refuses on a stale row via admit_draw's checks)."""
    try:
        agent = await get_agent_by_ref(creds, row.agent_obj_ref)
    except JuspayError as exc:
        logger.warning(f"uap: agent refresh failed {row.agent_obj_ref}: {exc}")
        return row
    action: Optional[Dict[str, Any]] = None
    action_id = row.action_id
    if agent is not None and not action_id:
        picked = pick_action(agent, row.action_obj_ref)
        action_id = picked.get("action_id") if picked else None
    if action_id:
        try:
            action = await get_action(creds, action_id)
        except JuspayError as exc:
            # Same posture as the agent lookup: a transport blip must never
            # be folded into the row (it would read as "no action" and
            # demote an ACTIVE mandate to PENDING).
            logger.warning(f"uap: action refresh failed {action_id}: {exc}")
            return row
    return await apply_juspay_records(row, agent, action)


# ---- Juspay /txns (agentic draw) ----


TEMPLATE_VERSION = "2.0"
PRICE_MODES = {"TAX_INCLUSIVE", "TAX_EXCLUSIVE"}
FULFILLED_BY = {"SELLER", "MARKETPLACE", "THIRD_PARTY"}
CHARGE_TYPES = {
    "DELIVERY",
    "PACKAGING",
    "CONVENIENCE",
    "PLATFORM_FEE",
    "SURGE",
    "TIP",
    "COD_FEE",
    "OTHER",
}
DISCOUNT_SCOPES = {"ORDER", "ITEM"}
DISCOUNT_FUNDED_BY = {"MERCHANT", "PLATFORM", "BRAND"}
TAX_TYPES = {"CGST", "SGST", "IGST", "CESS"}
FULFILMENT_TYPES = {"DELIVERY", "PICKUP", "DIGITAL", "SERVICE"}

_TWO_DECIMAL_RE = re.compile(r"^-?\d+\.\d{2}$")

# Pinned to the known-good sandbox curl (see create_txn).
# Longest order_id gateway 514 accepts (sandbox-probed 2026-08-27).
MAX_ORDER_ID_LEN = 27
# How long a draw request stays valid once sent.
PROPOSED_EXPIRY_SECONDS = 15 * 60


def prompt_hash(prompt: str) -> str:
    """``sha256:<base64url, unpadded>`` of the rider's request text — the
    ``agentic_payments.user_prompt_hash`` format the curl uses."""
    digest = hashlib.sha256(prompt.encode()).digest()
    return "sha256:" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _amount(value: Any, field_path: str, errors: List[str]) -> Decimal:
    if not isinstance(value, str) or not _TWO_DECIMAL_RE.match(value):
        errors.append(
            f"{field_path}: must be a decimal string with exactly two places, got {value!r}"
        )
        return Decimal("0")
    return Decimal(value)


def _require(
    container: Dict[str, Any],
    required_keys: List[str],
    field_path: str,
    errors: List[str],
) -> None:
    for key in required_keys:
        if key not in container:
            errors.append(f"{field_path}.{key}: required field missing")


def _obj(value: Any, field_path: str, errors: List[str]) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{field_path}: must be an object")
        return {}
    return value


def validate_canonical(canonical: Dict[str, Any], order_amount: str) -> None:
    if not isinstance(canonical, dict):
        raise ValueError(
            "items_canonical validation failed:\n  items_canonical: must be an object"
        )
    errors: List[str] = []

    if canonical.get("template_version") != TEMPLATE_VERSION:
        errors.append(f"template_version: must be {TEMPLATE_VERSION!r}")

    price_mode = canonical.get("price_mode")
    if price_mode not in PRICE_MODES:
        errors.append(f"price_mode: must be one of {sorted(PRICE_MODES)}")

    seller = _obj(canonical.get("seller"), "seller", errors)

    if "MIC" not in seller and "mic" not in seller:
        errors.append("seller.MIC: required field missing")
    _require(seller, ["legal_name", "fulfilled_by"], "seller", errors)
    if "fulfilled_by" in seller and seller["fulfilled_by"] not in FULFILLED_BY:
        errors.append(f"seller.fulfilled_by: must be one of {sorted(FULFILLED_BY)}")

    items = canonical.get("items") or []
    if not items:
        errors.append("items: at least one item required")

    computed_items_subtotal = Decimal("0")
    computed_line_discounts = Decimal("0")
    computed_line_taxes = Decimal("0")
    for item_index, item in enumerate(items):
        field_path = f"items[{item_index}]"
        if not isinstance(item, dict):
            errors.append(f"{field_path}: must be an object")
            continue
        _require(
            item,
            [
                "sku",
                "name",
                "qty",
                "uom",
                "unit_price",
                "line_gross",
                "line_discount",
                "line_tax",
                "line_total",
            ],
            field_path,
            errors,
        )
        item_name = item.get("name")
        if isinstance(item_name, str) and len(item_name.encode("utf-8")) > 128:
            errors.append(f"{field_path}.name: exceeds 128 bytes")
        line_gross = _amount(
            item.get("line_gross", ""), f"{field_path}.line_gross", errors
        )
        line_discount = _amount(
            item.get("line_discount", ""), f"{field_path}.line_discount", errors
        )
        line_total = _amount(
            item.get("line_total", ""), f"{field_path}.line_total", errors
        )
        _amount(item.get("unit_price", ""), f"{field_path}.unit_price", errors)
        item_tax_total = Decimal("0")
        for tax_index, tax_entry in enumerate(item.get("line_tax") or []):
            tax_path = f"{field_path}.line_tax[{tax_index}]"
            if not isinstance(tax_entry, dict):
                errors.append(f"{tax_path}: must be an object")
                continue
            if tax_entry.get("type") not in TAX_TYPES:
                errors.append(f"{tax_path}.type: must be one of {sorted(TAX_TYPES)}")
            item_tax_total += _amount(
                tax_entry.get("amount", ""), f"{tax_path}.amount", errors
            )
        computed_line_taxes += item_tax_total
        expected_line_total = line_gross - line_discount
        if price_mode == "TAX_EXCLUSIVE":
            expected_line_total += item_tax_total
        if line_total != expected_line_total:
            errors.append(
                f"{field_path}.line_total: {line_total} != line_gross - line_discount"
                f"{' + line_tax' if price_mode == 'TAX_EXCLUSIVE' else ''} ({expected_line_total})"
            )
        computed_items_subtotal += line_gross
        computed_line_discounts += line_discount

    computed_charges_total = Decimal("0")
    computed_charge_taxes = Decimal("0")
    for charge_index, charge in enumerate(canonical.get("charges") or []):
        field_path = f"charges[{charge_index}]"
        if not isinstance(charge, dict):
            errors.append(f"{field_path}: must be an object")
            continue
        _require(charge, ["type", "label", "amount", "tax_amount"], field_path, errors)
        if "type" in charge and charge["type"] not in CHARGE_TYPES:
            errors.append(f"{field_path}.type: must be one of {sorted(CHARGE_TYPES)}")
        computed_charges_total += _amount(
            charge.get("amount", ""), f"{field_path}.amount", errors
        )
        computed_charge_taxes += _amount(
            charge.get("tax_amount", ""), f"{field_path}.tax_amount", errors
        )

    computed_order_discounts = Decimal("0")
    for discount_index, discount_entry in enumerate(canonical.get("discounts") or []):
        field_path = f"discounts[{discount_index}]"
        if not isinstance(discount_entry, dict):
            errors.append(f"{field_path}: must be an object")
            continue
        _require(
            discount_entry,
            ["scope", "label", "amount", "funded_by"],
            field_path,
            errors,
        )
        if "scope" in discount_entry and discount_entry["scope"] not in DISCOUNT_SCOPES:
            errors.append(
                f"{field_path}.scope: must be one of {sorted(DISCOUNT_SCOPES)}"
            )
        if (
            "funded_by" in discount_entry
            and discount_entry["funded_by"] not in DISCOUNT_FUNDED_BY
        ):
            errors.append(
                f"{field_path}.funded_by: must be one of {sorted(DISCOUNT_FUNDED_BY)}"
            )
        computed_order_discounts += _amount(
            discount_entry.get("amount", ""), f"{field_path}.amount", errors
        )

    totals = _obj(canonical.get("totals"), "totals", errors)
    declared = {
        key: _amount(totals.get(key, ""), f"totals.{key}", errors)
        for key in [
            "items_subtotal",
            "discount_total",
            "charges_total",
            "tax_total",
            "round_off",
            "grand_total",
        ]
    }

    if not errors:
        if declared["items_subtotal"] != computed_items_subtotal:
            errors.append(
                f"totals.items_subtotal: {declared['items_subtotal']} != SUM(line_gross) {computed_items_subtotal}"
            )
        if (
            declared["discount_total"]
            != computed_line_discounts + computed_order_discounts
        ):
            errors.append(
                f"totals.discount_total: {declared['discount_total']} != "
                f"SUM(line_discount) + SUM(discounts.amount) {computed_line_discounts + computed_order_discounts}"
            )
        if declared["charges_total"] != computed_charges_total:
            errors.append(
                f"totals.charges_total: {declared['charges_total']} != SUM(charges.amount) {computed_charges_total}"
            )
        if declared["tax_total"] != computed_line_taxes + computed_charge_taxes:
            errors.append(
                f"totals.tax_total: {declared['tax_total']} != SUM(line_tax.amount) + "
                f"SUM(charges.tax_amount) {computed_line_taxes + computed_charge_taxes}"
            )
        if abs(declared["round_off"]) > Decimal("0.99"):
            errors.append(f"totals.round_off: |{declared['round_off']}| > 0.99")
        expected_grand_total = (
            declared["items_subtotal"]
            - declared["discount_total"]
            + declared["charges_total"]
            + declared["round_off"]
        )
        if price_mode == "TAX_EXCLUSIVE":
            expected_grand_total += declared["tax_total"]
        if declared["grand_total"] != expected_grand_total:
            errors.append(
                f"totals.grand_total: {declared['grand_total']} does not reconcile ({expected_grand_total}) under {price_mode}"
            )
        if not isinstance(order_amount, str) or not _TWO_DECIMAL_RE.match(order_amount):
            errors.append(
                f"order.amount: must be a decimal string with exactly two places, got {order_amount!r}"
            )
        elif declared["grand_total"] != Decimal(order_amount):
            errors.append(
                f"totals.grand_total: {declared['grand_total']} != order.amount {order_amount}"
            )

    fulfilment = _obj(canonical.get("fulfilment"), "fulfilment", errors)
    if fulfilment.get("type") not in FULFILMENT_TYPES:
        errors.append(f"fulfilment.type: must be one of {sorted(FULFILMENT_TYPES)}")
    if "deliver_by" not in fulfilment:
        errors.append('fulfilment.deliver_by: required (use "" when N/A)')

    if errors:
        raise ValueError("items_canonical validation failed:\n  " + "\n  ".join(errors))


async def create_txn(
    *,
    order_id: str,
    amount: str,
    items_canonical: Dict[str, Any],
    action_type: str = "INTENT",
    currency: str = "INR",
    customer_id: Optional[str] = None,
    proposed_expiry: Optional[str] = None,
    action_id: Optional[str] = None,
    user_prompt_hash: Optional[str] = None,
    redirect_after_payment: bool = True,
    # What the rider asked for, in words; hashed into user_prompt_hash when
    # the caller does not pass a hash of its own.
    prompt: Optional[str] = None,
    gateway_id: Optional[str] = None,
    gateway_reference_id: Optional[str] = None,
    api_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
    base_url: Optional[str] = None,
    # NY's own order fields from /confirm (``orderCreationReq``) — customer
    # contact, description, udfs, return_url and above all
    # ``metadata.webhook_url`` (NY's callback: this is how NY learns the order
    # is PAID and issues the tickets). Forwarded verbatim as ``order.<key>``.
    order_fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not merchant_id or not api_key:
        raise ValueError("merchant_id and api_key are required")
    if not action_id:
        raise ValueError("action_id is required: a draw must name the approved action")
    if not items_canonical:
        raise ValueError(
            "items_canonical is required: the cart is what the rider is charged for"
        )
    if len(order_id) > MAX_ORDER_ID_LEN:
        raise ValueError(
            f"order_id {order_id!r} is {len(order_id)} chars; the gateway rejects "
            f"anything over {MAX_ORDER_ID_LEN} with GATEWAY_BUSINESS_ERROR"
        )
    if action_type != "INTENT":
        raise ValueError(f"action_type must be INTENT, got {action_type!r}")
    validate_canonical(items_canonical, amount)
    if not user_prompt_hash:
        user_prompt_hash = prompt_hash(
            prompt or f"pay order {order_id} amount {amount}"
        )
    if not proposed_expiry:
        proposed_expiry = str(int(time.time()) + PROPOSED_EXPIRY_SECONDS)

    # Agentic /txns MUST create the order inline (``order.*``): sending a bare
    # ``order_id`` is rejected with "order is required for agentic payment",
    # and an id that already exists (NY's /confirm order) is DUPLICATE_ORDER_ID.
    # Settling an NY-created order is therefore an open Juspay/NY question.
    body: Dict[str, Any] = {
        "order.order_id": order_id,
        "order.amount": amount,
        "order.currency": currency,
        "merchant_id": merchant_id,
        "payment_method_type": "UPI",
        "payment_method": "AUTONOMOUS",
        "format": "json",
        "redirect_after_payment": redirect_after_payment,
        "agentic_payments.action_type": action_type,
        "agentic_payments.action_id": action_id,
        "agentic_payments.modality": "AUTONOMOUS",
        "agentic_payments.user_prompt_hash": user_prompt_hash,
        "agentic_payments.proposed_expiry": proposed_expiry,
        "agentic_payments.items_canonical": items_canonical,
        "gateway_id": gateway_id or EULER_GATEWAY_ID,
    }
    if customer_id:
        body["order.customer_id"] = customer_id
    for key, value in (order_fields or {}).items():
        if value in (None, ""):
            continue
        # Never let NY's copy override the three fields the draw owns.
        if key in {"order_id", "amount", "currency", "customer_id"}:
            continue
        body[f"order.{key}"] = value
    if gateway_reference_id:
        body["gateway_reference_id"] = gateway_reference_id

    logger.info(
        f"uap txns: draw order_id={order_id} amount={amount} action_id={action_id}"
    )
    logger.debug(f"uap txns: request body={_redact(body)}")
    txn_response = await request(
        "POST",
        "/txns",
        api_key=api_key,
        merchant_id=merchant_id,
        base_url=base_url,
        json_body=body,
    )

    logger.debug(f"uap txns: response={_redact(txn_response)}")
    logger.info(
        f"uap txns: order_id={order_id} status={txn_response.get('status')} "
        f"txn_id={txn_response.get('txn_id')} txn_uuid={txn_response.get('txn_uuid')}"
    )
    return txn_response


# ---- NammaYatri multimodal ----


_NY_TIMEOUT_SECONDS = 15
# initiate is polled (1 s apart) until pricing lands; NY doc says 1 s repeats.
_NY_PRICING_POLLS = 6


def normalize_amount(value: Any) -> Optional[str]:
    """NY fares are numbers (40.0); the wire wants a two-decimal string."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    except InvalidOperation:
        return None


async def initiate_journey(
    ny_base: str, rider_token: str, journey_id: str
) -> Dict[str, Any]:
    """POST /v2/multimodal/{journey_id}/initiate - exact fares for one journey.

    NOT a plain read: initiate starts the journey on NY's side. The chat's
    details tool already called it once; the draw calls it again right
    before confirm so the legs it confirms are the ones NY holds now. NY
    treats the repeat as a refresh (verified 2026-09-03)."""
    base = ny_base.rstrip("/").removesuffix("/v2")
    url = f"{base}/v2/multimodal/{journey_id}/initiate"
    logger.info(f"uap ny: initiate journey={journey_id}")
    # NY doc, step 2: repeat initiate until every bookable leg carries a
    # pricingId — confirming an unpriced leg fails.
    body: Dict[str, Any] = {}
    for attempt in range(_NY_PRICING_POLLS):
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
        unpriced = [
            leg
            for leg in body.get("legs") or []
            if leg.get("bookingAllowed") and not leg.get("pricingId")
        ]
        if not unpriced:
            return body
        logger.info(
            f"uap ny: initiate journey={journey_id} {len(unpriced)} leg(s) unpriced, "
            f"poll {attempt + 1}/{_NY_PRICING_POLLS}"
        )
        await asyncio.sleep(1.0)
    return body


async def confirm_journey(
    ny_base: str,
    rider_token: str,
    journey_id: str,
    journey: Dict[str, Any],
    tickets: int = 1,
) -> Dict[str, Any]:
    """POST /v2/multimodal/{journey_id}/confirm - tell NY to book the tickets.

    One element per leg: skipBooking for non-bookable (walk) legs, one adult
    ticket on each bookable leg. NY responds with orderSdkPayload carrying
    ITS payment order (orderId + final amount) - the order that must turn
    PAID for tickets to be issued.

    ``skipCreateOrderCall=true``: NY still allocates its order (id + amount)
    but does NOT create it on Juspay via /session. The agentic /txns creates
    the order inline; without the skip the two creations collide
    (DUPLICATE_ORDER_ID, or "order is required for agentic payment" when
    /txns is sent a bare id). Param name per Juspay/NY, 2026-09-02.
    """
    elements = []
    for leg in journey.get("legs") or []:
        bookable = bool(leg.get("bookingAllowed"))
        elements.append(
            {
                "journeyLegOrder": leg.get("order"),
                "skipBooking": not bookable,
                "ticketQuantity": tickets if bookable else None,
                "childTicketQuantity": 0 if bookable else None,
                # Left null on purpose: NY defaults the ticket category to
                # ADULT (live confirm 2026-09-06 booked adultTicketQuantity=1
                # with null here). The doc's ``[{name, quantity}]`` shape does
                # not match the live category keys (categoryName / categoryId),
                # so naming a category is riskier than letting NY default.
                "categorySelectionReq": None,
                "crisSdkResponse": None,
                "vehicleNumber": None,
                "tripId": None,
                "seatIds": None,
            }
        )
    body = {"enableOffer": None, "journeyConfirmReqElements": elements}
    base = ny_base.rstrip("/").removesuffix("/v2")
    url = f"{base}/v2/multimodal/{journey_id}/confirm?skipCreateOrderCall=true"
    logger.info(f"uap ny: confirm journey={journey_id} tickets={tickets}")
    logger.debug(f"uap ny: confirm body={body}")
    async with create_aiohttp_session() as session:
        async with session.post(
            url,
            json=body,
            headers={"token": rider_token, "Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=_NY_TIMEOUT_SECONDS),
        ) as response:
            payload = await response.json(content_type=None)
            logger.info(
                f"uap ny: confirm journey={journey_id} -> HTTP {response.status} "
                f"result={payload.get('result') if isinstance(payload, dict) else None}"
            )
            logger.debug(f"uap ny: confirm response={_redact(payload)}")
            if response.status >= 400:
                raise RuntimeError(f"NY confirm HTTP {response.status}: {payload}")
            return payload


def confirm_order_id(confirm: Dict[str, Any]) -> Optional[str]:
    """``orderSdkPayload.order_id`` — the Juspay order NY created."""
    sdk_payload = confirm.get("orderSdkPayload") or {}
    value = sdk_payload.get("order_id") or sdk_payload.get("orderId")
    if not value:
        # ``skipCreateOrderCall=true`` (2026-09-03): NY allocates the order
        # but hands back the CREATE request it skipped — ``orderCreationReq``
        # — with ``orderSdkPayload`` null. Same order id, different envelope.
        creation = confirm.get("orderCreationReq") or {}
        value = creation.get("order_id") or creation.get("orderId")
    return str(value) if value else None


# Keys of NY's ``orderCreationReq`` that describe the order itself and are
# safe to forward to /txns as ``order.<key>``. Everything SDK-specific
# (``action``, ``payment_page_client_id``, ``basket``) stays behind.
_FORWARDED_ORDER_KEYS = {
    "customer_email",
    "customer_phone",
    "description",
    "return_url",
    "udf1",
    "udf2",
    "udf3",
    "udf4",
    "udf5",
    "udf6",
    "udf7",
    "udf8",
    "udf9",
    "udf10",
}


def confirm_order_fields(confirm: Dict[str, Any]) -> Dict[str, Any]:
    """NY's order description from the skipped create request: contact,
    description, udfs, return_url and ``metadata.webhook_url`` (NY's Juspay
    callback — without it NY never hears the order was paid).

    ONLY that one metadata key is forwarded. NY's confirm also carries
    ``metadata.split_settlement_details`` (an object) and
    ``metadata.AXIS_BIZ:remarks``; the agentic /txns route cannot decode
    those and answers "Internal decode error" (sandbox, 2026-09-06). Empty
    when the confirm carried none."""
    creation = confirm.get("orderCreationReq") or {}
    out: Dict[str, Any] = {}
    for key, value in creation.items():
        if value in (None, ""):
            continue
        if key in _FORWARDED_ORDER_KEYS or key == "metadata.webhook_url":
            out[key] = value
    return out


def confirm_gateway_reference_id(confirm: Dict[str, Any]) -> Optional[str]:
    value = confirm.get("gatewayReferenceId")
    return str(value) if value else None


def confirm_amount(confirm: Dict[str, Any]) -> Optional[str]:
    """Final fare from the confirm's sdk payload (or the skipped order
    request), two-decimal string."""
    sdk_payload = confirm.get("orderSdkPayload") or {}
    inner = sdk_payload.get("sdk_payload") or sdk_payload.get("sdkPayload") or {}
    amount = (inner.get("payload") or {}).get("amount")
    if amount in (None, ""):
        amount = (confirm.get("orderCreationReq") or {}).get("amount")
    return normalize_amount(amount)


async def _ny_get(ny_base: str, rider_token: str, path: str) -> Dict[str, Any]:
    base = ny_base.rstrip("/").removesuffix("/v2")
    url = f"{base}{path}"
    logger.debug(f"uap ny: GET {path}")
    async with create_aiohttp_session() as session:
        async with session.get(
            url,
            headers={"token": rider_token, "Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=_NY_TIMEOUT_SECONDS),
        ) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(f"NY {path} HTTP {response.status}: {body}")
            return body


async def booking_info(
    ny_base: str, rider_token: str, journey_id: str
) -> Dict[str, Any]:
    """GET /v2/multimodal/{journey_id}/booking/info — tickets after payment."""
    return await _ny_get(
        ny_base, rider_token, f"/v2/multimodal/{journey_id}/booking/info"
    )


async def payment_status(ny_base: str, rider_token: str, journey_id: str) -> str:
    """GET /v2/multimodal/{journey_id}/booking/paymentStatus → NEW|PENDING|PAID|FAILED.

    NY answers ``{"paymentOrder": {"status": "PENDING", ...}, "journeyId": …}``
    (verified 2026-09-06); older builds used a top-level ``paymentStatus``,
    still read as a fallback. Before the Juspay order exists NY answers 200
    with ``{"errorCode": "INTERNAL_ERROR"}`` — that is "not yet", so it comes
    back as "" and the callers treat it like NEW."""
    body = await _ny_get(
        ny_base, rider_token, f"/v2/multimodal/{journey_id}/booking/paymentStatus"
    )
    order = body.get("paymentOrder")
    value = order.get("status") if isinstance(order, dict) else None
    return str(value or body.get("paymentStatus") or "").upper()


# NY PaymentStatus enum (doc): NEW | PENDING | PAID | FAILED | REFUNDED |
# NOT_APPLICABLE (no payment needed — tickets are issued without one).
PAID_STATUSES = {"PAID", "CHARGED", "SUCCESS", "NOT_APPLICABLE"}
FAILED_STATUSES = {"FAILED", "FAILURE", "CANCELLED", "REFUNDED"}


async def wait_for_payment(
    ny_base: str,
    rider_token: str,
    journey_id: str,
    *,
    max_wait_seconds: float,
    step_seconds: float = 2.5,
) -> str:
    """Poll NY's paymentStatus until it is terminal or the window closes.
    Returns the last status seen ("" when NY never answered one). A
    transport error on one poll is not terminal — the next tick retries."""
    deadline = time.monotonic() + max_wait_seconds
    last = ""
    while True:
        try:
            last = await payment_status(ny_base, rider_token, journey_id)
        except Exception as exc:  # one bad poll must not end the wait
            logger.warning(
                f"uap ny: paymentStatus poll failed journey={journey_id}: {exc}"
            )
        if last in PAID_STATUSES or last in FAILED_STATUSES:
            return last
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            return last
        await asyncio.sleep(min(step_seconds, remaining_s))


# ---- onboarding expiry watcher ----
# Backend polling is the source of truth: Juspay's sandbox does not
# reliably deliver AGENT_* webhooks, so every attempt is polled on
# INTERVAL_SECONDS until it settles or the onboarding window closes.
# Webhooks, when they arrive, are a bonus signal.
INTERVAL_SECONDS = 15
TIMEOUT_SECONDS = 30 * 60

_tasks: Dict[str, "asyncio.Task[None]"] = {}


async def _lock(agent_obj_ref: str) -> bool:
    if not is_redis_configured():
        return agent_obj_ref not in _tasks
    try:
        client = await (await get_redis_service()).get_client()
        return bool(
            await client.set(
                f"uap:poll:{agent_obj_ref}", "1", nx=True, ex=TIMEOUT_SECONDS + 60
            )
        )
    except Exception as exc:
        logger.warning(f"uap poll lock unavailable ({exc}); running unlocked")
        return agent_obj_ref not in _tasks


async def _unlock(agent_obj_ref: str) -> None:
    if not is_redis_configured():
        return
    try:
        client = await (await get_redis_service()).get_client()
        await client.delete(f"uap:poll:{agent_obj_ref}")
    except Exception:
        pass


async def _run(creds: JuspayCredentials, agent_obj_ref: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TIMEOUT_SECONDS
    try:
        while loop.time() < deadline:
            await asyncio.sleep(
                min(float(INTERVAL_SECONDS), max(deadline - loop.time(), 0.0))
            )
            try:
                row = await get_by_agent_obj_ref(agent_obj_ref)
                if row is None or row.status in TERMINAL_STATUSES:
                    return
                updated = await refresh_agent(creds, row)
                if updated is None or updated.status in TERMINAL_STATUSES:
                    return
            except Exception:  # one bad tick (DB / network) must not end the watch
                logger.opt(exception=True).warning(
                    f"uap poll {agent_obj_ref}: tick failed, retrying"
                )
        row = await get_by_agent_obj_ref(agent_obj_ref)
        if row is not None and row.status == "PENDING":
            await apply_juspay_records(row, None)
            logger.info(f"uap poll {agent_obj_ref}: window closed -> EXPIRED")
    except Exception:
        logger.opt(exception=True).error(f"uap poll {agent_obj_ref}: watcher died")
    finally:
        _tasks.pop(agent_obj_ref, None)
        await _unlock(agent_obj_ref)


async def start_agent_poll(creds: JuspayCredentials, agent_obj_ref: str) -> bool:
    """Begin watching this attempt. False if a watcher already holds it."""
    if not await _lock(agent_obj_ref):
        return False
    _tasks[agent_obj_ref] = asyncio.create_task(_run(creds, agent_obj_ref))
    logger.info(f"uap poll {agent_obj_ref}: started")
    return True
