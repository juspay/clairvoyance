"""Builders and validators for agentic payments: the items_canonical cart
and the intent constraints (the mandate rule)."""

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from app.services.preview.uap.api import TEMPLATE_VERSION

# ---- ticket cart canonical ----


_TWO = Decimal("0.01")


def _money(value: Decimal) -> str:
    return str(value.quantize(_TWO, rounding=ROUND_HALF_UP))


def build_ticket_cart(
    *,
    journey_id: str,
    total_fare: str,
    tickets: int,
    route_label: str,
    operator_name: str,
    operator_mic: str,
) -> Dict[str, Any]:
    """One ticket line, tax-inclusive, totals reconciled.

    ``total_fare`` is the amount the draw charges — NY's confirmed fare for
    all ``tickets`` — so the cart's grand_total always equals the order
    amount the validator checks it against. The unit price is derived.

    ``operator_mic`` is the merchant identification code the acquirer
    verifies — it must match what Juspay holds for CMRL / MTC, or the draw
    is refused at their end with a message the rider cannot act on.
    """
    if tickets < 1:
        raise ValueError("tickets must be >= 1")
    gross = Decimal(total_fare)
    unit = gross / tickets
    total = _money(gross)

    return {
        "template_version": TEMPLATE_VERSION,
        "price_mode": "TAX_INCLUSIVE",
        "seller": {
            "MIC": operator_mic,
            "legal_name": operator_name,
            "fulfilled_by": "SELLER",
        },
        "items": [
            {
                "sku": f"ticket:{journey_id}",
                # The validator caps name at 128 bytes; a route label with
                # station names can run long.
                "name": route_label.encode("utf-8")[:120].decode("utf-8", "ignore"),
                "qty": tickets,
                "uom": "EA",
                "unit_price": _money(unit),
                "line_gross": total,
                "line_discount": "0.00",
                "line_tax": [],
                "line_total": total,
            }
        ],
        "charges": [],
        "discounts": [],
        # deliver_by is required by the validator even when N/A ("" per its
        # own message); a ticket is fulfilled at the gate, not delivered.
        "fulfilment": {"type": "DIGITAL", "deliver_by": ""},
        "totals": {
            "items_subtotal": total,
            "discount_total": "0.00",
            "charges_total": "0.00",
            "tax_total": "0.00",
            "round_off": "0.00",
            "grand_total": total,
        },
    }


class IntentConstraints(BaseModel):
    """The standing rule the rider approves — the whole product surface.

    Every scalar except ``max_draws`` is a STRING on the wire — Juspay's
    AOP API rejects the action create (HTTP 400) when amounts or times
    arrive as JSON numbers; ``max_draws`` is a count and goes as an int.
    Amounts are decimal rupee strings ("2500.00"), not paise; times are
    10-digit epoch-SECOND strings ("1755330900"). Getting any of these wrong
    is silently accepted for the agent and rejected for the action. Field
    set = Juspay's agenticCheckout doc (2026-09-05): nothing extra is sent.
    """

    binding_type: Literal["VPA_LIST", "VERIFIED_NAMES", "MCC_CAPS"]
    # Populate the one matching binding_type; the others go up as empty
    # arrays rather than being omitted.
    bound_vpas: List[str] = Field(default_factory=list)
    bound_verified_names: List[str] = Field(default_factory=list)
    bound_mcc: List[str] = Field(default_factory=list)

    max_per_draw: str
    max_total: str
    max_draws: Optional[int] = None

    # 10-digit epoch-second strings, not ints — see the class docstring.
    valid_from: str
    valid_till: str

    # AUTO is the reason to build agentic at all — no tap per purchase.
    # CONFIRM reintroduces the approval it exists to remove.
    draw_confirm: Literal["AUTO", "CONFIRM"] = "AUTO"


# ---- intent constraints ----


def build_transit_intent(
    verified_names: List[str],
    *,
    max_per_draw: str,
    max_total: str,
    max_draws: int,
    validity_days: int,
    valid_from: Optional[datetime] = None,
) -> IntentConstraints:
    """The rule for a transit agent. Every number comes from the merchant's
    template config (``configurations.agentic_payments.limits``), possibly
    overlaid by the rider's choice — this code holds no defaults.

    ``VERIFIED_NAMES`` rather than ``MCC_CAPS`` on purpose. An MCC would
    authorise an entire merchant category — every transport operator in the
    country — where the rider only ever meant two named operators. Naming
    them is the tightest binding that still works, and the names are
    AE-verified, so they cannot be spoofed by a merchant claiming the label.

    ``draw_confirm=AUTO`` is what makes this worth building: with CONFIRM the
    rider taps to approve every ticket, which is the friction agentic
    payments exist to remove. That is a product decision, and it lives here
    rather than being buried in a payload.
    """
    if not verified_names:
        raise ValueError(
            "verified_names cannot be empty — an unbound intent authorises "
            "spending at any merchant"
        )

    start = valid_from or datetime.now(timezone.utc)
    end = start + timedelta(days=validity_days)

    return IntentConstraints(
        binding_type="VERIFIED_NAMES",
        bound_verified_names=verified_names,
        # Sent as empty arrays rather than omitted — the spec asks for the
        # unused bindings to be present and empty.
        bound_vpas=[],
        bound_mcc=[],
        max_per_draw=max_per_draw,
        max_total=max_total,
        max_draws=max_draws,
        # 10-digit epoch-SECOND strings ("1755330900") — the same format the
        # known-good /txns curl uses for proposed_expiry. The 13-digit
        # millisecond strings we sent first are rejected on the action create
        # ("invalid intentConstraints.validTill: input contains invalid
        # characters", HTTP 400 INTENT_AUTH_FAILURE, sandbox 2026-09-02).
        valid_from=_epoch_seconds(start),
        valid_till=_epoch_seconds(end),
        draw_confirm="AUTO",
    )


def _epoch_seconds(when: datetime) -> str:
    """10-digit epoch-SECOND string. 13-digit milliseconds are rejected."""
    return str(int(when.timestamp()))
