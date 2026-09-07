"""Leaf shapes for the agentic module (module rules §1). Imports nothing
internal — db/decoder.py is the only place a row becomes one of these.

An *agent* is a rider's standing UPI mandate held by Juspay (AOP); an
*action* is the intent under it that draws are made against. One row per
onboarding attempt; a rider may have many rows over time, one drawable.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class CrmCustomerAgent(BaseModel):
    """One onboarding attempt for one customer — one element of
    ``crm_customer.attributes["agents"]`` (several per rider). Everything
    Juspay mints is Optional: the entry exists before
    the consent screen opens and is filled in by result / poll / webhook
    in whatever order they land."""

    id: str
    merchant_id: str
    customer_id: str
    juspay_customer_id: Optional[str] = None

    agent_obj_ref: str
    action_obj_ref: Optional[str] = None

    agent_id: Optional[str] = None
    agent_ref_id: Optional[str] = None
    action_id: Optional[str] = None
    action_ref_id: Optional[str] = None
    payer_avpa: Optional[str] = None
    agentic_app: Optional[str] = None

    status: str = "PENDING"
    action_status: Optional[str] = None
    # The rule as JUSPAY holds it after approval (rider-edited), or our
    # proposal until then. Amounts are decimal strings, times epoch-ms strings.
    intent_constraints: Optional[Dict[str, Any]] = None
    meta: Dict[str, Any] = Field(default_factory=dict)
    verified_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # The rider's pick when several agents are ACTIVE; the booking path
    # charges the preferred one first.
    preferred: bool = False

    @property
    def has_all_identifiers(self) -> bool:
        """agent_id + action_id are what ``/txns`` draws against; payer_avpa
        (the rider's UPI id, from the SDK result) is the proof consent
        finished — a row without it never reached the end of onboarding."""
        return bool(self.agent_id and self.action_id and self.payer_avpa)

    @property
    def is_drawable(self) -> bool:
        return (
            self.status == "ACTIVE"
            and (self.action_status or "").upper() == "ACTIVE"
            and self.has_all_identifiers
        )


class DrawUsage(BaseModel):
    """What an agent has consumed — CHARGED draws plus PENDING ones in
    flight (they reserve their amount until they settle). Gathered by the
    session ledger (app/services/uap/ledger.py); decided on here."""

    drawn_total: Decimal = Decimal("0.00")
    draw_count: int = 0
