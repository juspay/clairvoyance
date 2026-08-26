import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.services.uap import client

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

HARDCODED_SELLER = {
    "MIC": "NAMMAYATRIMIC001",
    "legal_name": "Moving Tech Innovations Private Limited",
    "fulfilled_by": "SELLER",
}


def build_canonical(
    amount: str,
    *,
    sku: str = "pricing-uuid-001",
    name: str = "Metro GUINDY-CHENNAI-CENTRAL (CMRL-BLUE) - ADULT",
    journey_id: str = "journey-uuid-001",
    leg_id: str = "leg-uuid-001",
    provider: str = "CMRL",
    order_ref: str = "ny-journey-uuid-001",
) -> Dict[str, Any]:
    """Hardcoded canonical for the NammaYatri txns phase: one qty-1 ticket
    line whose money fields all mirror ``amount``, so it always reconciles.
    Only what NammaYatri returns flows in; the rest stays fixed until the
    real cart builder lands."""
    return {
        "template_version": TEMPLATE_VERSION,
        "price_mode": "TAX_INCLUSIVE",
        "merchant_order_ref": order_ref,
        "seller": dict(HARDCODED_SELLER),
        "items": [
            {
                "sku": sku,
                "name": name,
                "qty": 1,
                "uom": "UNIT",
                "unit_price": amount,
                "line_gross": amount,
                "line_discount": "0.00",
                "line_tax": [],
                "line_total": amount,
                "attributes": {
                    "journey_id": journey_id,
                    "journey_leg_id": leg_id,
                    "provider_name": provider,
                },
            }
        ],
        "charges": [],
        "discounts": [],
        "totals": {
            "items_subtotal": amount,
            "discount_total": "0.00",
            "charges_total": "0.00",
            "tax_total": "0.00",
            "round_off": "0.00",
            "grand_total": amount,
        },
        "fulfilment": {"type": "DIGITAL", "deliver_by": ""},
    }


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
    mcc: Optional[str] = None,
    proposed_expiry: Optional[str] = None,
    payer_avpa: Optional[str] = None,
    action_ref_id: Optional[str] = None,
    action_id: Optional[str] = None,
    user_prompt_hash: Optional[str] = None,
    webhook_url: Optional[str] = None,
    redirect_after_payment: bool = True,
    txn_type: Optional[str] = None,
    gateway_id: Optional[str] = None,
    gateway_reference_id: Optional[str] = None,
    x_feature: Optional[str] = None,
    euler_api_gateway: Optional[str] = None,
    service_type: Optional[str] = None,
    api_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    if action_type != "INTENT":
        raise ValueError(f"action_type must be INTENT, got {action_type!r}")
    if not (action_ref_id or action_id):
        raise ValueError("INTENT requires action_ref_id or action_id")
    if action_ref_id and not payer_avpa:
        raise ValueError("INTENT with action_ref_id requires payer_avpa")

    if webhook_url and not webhook_url.startswith("https://"):
        raise ValueError("webhook_url must be HTTPS")

    validate_canonical(items_canonical, amount)

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
        "agentic_payments.modality": "AUTONOMOUS",
        "agentic_payments.items_canonical": items_canonical,
    }
    optional_fields = {
        "txn_type": txn_type,
        "gateway_id": gateway_id,
        "order.metadata.YES_BIZ:gateway_reference_id": gateway_reference_id,
        "order.customer_id": customer_id,
        "agentic_payments.mcc": mcc,
        "agentic_payments.proposed_expiry": proposed_expiry,
        "agentic_payments.payer_avpa": payer_avpa,
        "agentic_payments.action_ref_id": action_ref_id,
        "agentic_payments.action_id": action_id,
        "agentic_payments.user_prompt_hash": user_prompt_hash,
        "metadata.webhook_url": webhook_url,
    }
    body.update(
        {field: value for field, value in optional_fields.items() if value is not None}
    )

    logger.info(f">>> Juspay /txns {action_type} order_id={order_id} amount={amount}")
    logger.info(f">>> Juspay /txns request body={body}")
    gateway_headers = {
        header: value
        for header, value in {
            "x-feature": x_feature,
            "custom-euler-api-gateway": euler_api_gateway,
            "x-service-type": service_type,
        }.items()
        if value
    }
    txn_response = await client.request(
        "POST",
        "/txns",
        api_key=api_key,
        merchant_id=merchant_id,
        base_url=base_url,
        json_body=body,
        extra_headers=gateway_headers or None,
    )

    logger.info(f">>> Juspay /txns full response={txn_response}")
    logger.info(
        f">>> Juspay /txns ok order_id={txn_response.get('order_id')} "
        f"status={txn_response.get('status')} txn_uuid={txn_response.get('txn_uuid')} "
        f"sub_ref_id={(txn_response.get('agentic_payments') or {}).get('sub_ref_id')}"
    )
    return txn_response
