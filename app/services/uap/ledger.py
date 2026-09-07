"""The ticket-payment ledger: one element per Juspay order, kept on the
chat session the booking happened in (``chat_session.metadata.uap_draws``).

Why the session and not a table of its own: the chat is where a draw
happens and where its ticket is polled, so the row is already in hand,
and a rider's usage against an agent is a sum over that rider's sessions
(matched on the ``rider_ref`` template var the bind writes). The
append-only mirror in ``crm_event_raw`` stays the audit trail.

The decisions (is this draw allowed; what is left) stay pure in
``app.crm.agentic`` — this module gathers the usage and applies the writes.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.core.logger import logger
from app.crm.agentic.contracts import CrmCustomerAgent, DrawUsage, check_draw, remaining
from app.crm.record.contracts import record_event
from app.database.accessor.breeze_buddy.uap_session import (
    get_agent_usage,
    settle_session_draw,
    upsert_session_draw,
)

EVENT_SOURCE = "agentic-upi"
TOPIC_DRAW = "agentic.draw"

# Our status of a draw, and the NY paymentStatus values that settle it.
DRAW_STATUSES = ("CHARGED", "PENDING", "FAILED", "REFUSED")
# NY PaymentStatus enum: PAID settles the draw; FAILED / REFUNDED fail it;
# NOT_APPLICABLE (no payment needed) never touches our status.
_NY_PAID = {"PAID", "CHARGED", "SUCCESS"}
_NY_FAILED = {"FAILED", "FAILURE", "CANCELLED", "REFUNDED"}


class SessionDraw(BaseModel):
    """One element of ``metadata.uap_draws``."""

    agent_ref: str
    order_id: str
    journey_id: Optional[str] = None
    tickets: Optional[int] = None
    txn_id: Optional[str] = None
    amount: str
    status: str
    # NY's own view of the order, as last polled (NEW | PENDING | PAID | FAILED).
    ny_payment_status: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def settled_status(current: str, ny_status: str) -> str:
    """PURE: our draw status after a NY paymentStatus poll. PAID settles a
    PENDING draw to CHARGED, FAILED to FAILED; anything else, or a draw
    that is not PENDING, is left as it is."""
    if current != "PENDING":
        return current
    ny = (ny_status or "").upper()
    if ny in _NY_PAID:
        return "CHARGED"
    if ny in _NY_FAILED:
        return "FAILED"
    return current


async def _emit(
    merchant_id: str, customer_id: str, key: str, payload: Dict[str, Any]
) -> None:
    try:
        await record_event(
            merchant_id=merchant_id,
            source=EVENT_SOURCE,
            topic=TOPIC_DRAW,
            external_id=key,
            payload=payload,
            customer_id=customer_id,
        )
    except Exception:  # mirror posture: a lost event never breaks the flow
        logger.opt(exception=True).error(f"uap ledger: event {key} not recorded")


async def usage(
    merchant_id: str, customer_id: str, agent: CrmCustomerAgent
) -> DrawUsage:
    total, count = await get_agent_usage(merchant_id, customer_id, agent.agent_obj_ref)
    try:
        drawn = Decimal(total).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        drawn = Decimal("0.00")
    return DrawUsage(drawn_total=drawn, draw_count=count)


async def admit_draw(
    merchant_id: str, customer_id: str, agent: CrmCustomerAgent, amount: str
) -> Optional[str]:
    """None if this charge is allowed against the approved rule and the
    ledger so far; else the refusal reason. The caller refreshes the rule
    from Juspay first."""
    return check_draw(agent, await usage(merchant_id, customer_id, agent), amount)


async def status_view(
    merchant_id: str, customer_id: str, agent: CrmCustomerAgent
) -> Dict[str, Any]:
    return remaining(agent, await usage(merchant_id, customer_id, agent))


async def record_draw(
    session_id: str,
    agent: CrmCustomerAgent,
    *,
    order_id: str,
    amount: str,
    status: str,
    journey_id: str,
    tickets: int,
    txn_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> SessionDraw:
    """Ledger + timeline entry for one charge attempt (any outcome). A
    second write for the same order merges into the first element — the
    session row serialises them, so one order is never charged twice."""
    if status not in DRAW_STATUSES:
        raise ValueError(f"unknown draw status {status!r}")
    stamp = _now()
    element = SessionDraw(
        agent_ref=agent.agent_obj_ref,
        order_id=order_id,
        journey_id=journey_id,
        tickets=tickets,
        txn_id=txn_id,
        amount=amount,
        status=status,
        meta=meta or {},
        created_at=stamp,
        updated_at=stamp,
    )
    patch: Dict[str, Any] = {"status": status, "updated_at": stamp}
    if txn_id:
        patch["txn_id"] = txn_id
    if meta:
        patch["meta"] = meta
    draws = await upsert_session_draw(session_id, order_id, element.model_dump(), patch)
    await _emit(
        agent.merchant_id,
        agent.customer_id,
        f"{order_id}:{status}",
        {
            "order_id": order_id,
            "txn_id": txn_id,
            "amount": amount,
            "status": status,
            "journey_id": journey_id,
            "tickets": tickets,
            "agent_obj_ref": agent.agent_obj_ref,
            "session_id": session_id,
            **(meta or {}),
        },
    )
    stored = next((d for d in draws if d.get("order_id") == order_id), None)
    return SessionDraw(**stored) if stored else element


async def settle_draw(
    session_id: str,
    journey_id: str,
    ny_status: str,
    *,
    merchant_id: str,
    customer_id: str,
) -> List[SessionDraw]:
    """What a NY paymentStatus poll learned, written back: the NY status
    always, and our status when it settles (PENDING -> CHARGED / FAILED).
    Returns the session's draws for this journey afterwards."""
    ny = (ny_status or "").upper()
    if not ny:
        return []
    stamp = _now()
    # Read what is there first so the status transition is per element.
    before = await settle_session_draw(session_id, journey_id, {})
    mine = [d for d in before if d.get("journey_id") == journey_id]
    if not mine:
        return []
    after: List[SessionDraw] = []
    for d in mine:
        new_status = settled_status(str(d.get("status") or ""), ny)
        patch = {"ny_payment_status": ny, "updated_at": stamp, "status": new_status}
        draws = await settle_session_draw(session_id, journey_id, patch)
        if new_status != d.get("status"):
            await _emit(
                merchant_id,
                customer_id,
                f"{d.get('order_id')}:{new_status}",
                {
                    "order_id": d.get("order_id"),
                    "txn_id": d.get("txn_id"),
                    "amount": d.get("amount"),
                    "status": new_status,
                    "ny_payment_status": ny,
                    "journey_id": journey_id,
                    "agent_obj_ref": d.get("agent_ref"),
                    "session_id": session_id,
                },
            )
        after = [SessionDraw(**x) for x in draws if x.get("journey_id") == journey_id]
    return after
