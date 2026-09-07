"""agentic payment agent — the rider's standing UPI rule: Juspay's agent + action
records -> our row, and what may be drawn against it.

PURE decisions first (translate Juspay vocabulary to ours; draw admission),
then gather -> decide -> apply. Imported only via contracts.py. The draw
LEDGER is not here: draws live on the chat session they happened in
(app/services/uap/ledger.py), which gathers the usage and calls the pure
``check_draw`` / ``remaining`` below.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, Optional

from app.core.logger import logger
from app.crm.preview.uap.db import accessor
from app.crm.preview.uap.schemas import CrmCustomerAgent, DrawUsage
from app.crm.record.contracts import record_event

# ---- translation: Juspay vocabulary -> ours (pure) ----
TERMINAL_STATUSES = frozenset({"ACTIVE", "EXPIRED", "REVOKED", "FAILED"})


def pick_action(
    agent: Optional[Dict[str, Any]], action_obj_ref: Optional[str]
) -> Optional[Dict[str, Any]]:
    """The action on this agent that is OURS — matched on the reference we
    minted, nothing else. Fail closed: with no ref, or no match, there is
    no action to draw against (a foreign action on the same customer must
    never be adopted)."""
    if not agent or not action_obj_ref:
        return None
    for action in agent.get("actions") or []:
        if (
            isinstance(action, dict)
            and action.get("object_reference_id") == action_obj_ref
        ):
            return action
    return None


def derive_status(
    agent: Optional[Dict[str, Any]], action: Optional[Dict[str, Any]]
) -> str:
    """Our one-word status from Juspay's agent + action records."""
    if agent is None:
        return "EXPIRED"
    a_status = str(agent.get("status") or "").upper()
    if a_status == "PAUSED":
        return "PAUSED"
    if a_status in {"REVOKED", "DEACTIVATED"}:
        return "REVOKED"
    if action is None:
        return "PENDING"
    x_status = str(action.get("status") or "").upper()
    if x_status in {"EXPIRED", "REVOKED", "FAILED"}:
        return x_status  # one to one: the ledger keeps the real reason
    if a_status == "ACTIVE" and x_status == "ACTIVE":
        return "ACTIVE"
    return "PENDING"


def plan_patch(
    current_status: str,
    agent: Optional[Dict[str, Any]],
    action: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """DECIDE: the column patch a refresh implies. Never demotes a rider
    decision: PAUSED/REVOKED stay unless Juspay now says ACTIVE again — or
    the agent is gone altogether (``agent=None``), which is EXPIRED whatever
    the rider had chosen. ``verified_at`` is stamped by the caller."""
    status = derive_status(agent, action)
    if (
        agent is not None
        and current_status in {"PAUSED", "REVOKED"}
        and status != "ACTIVE"
    ):
        status = current_status
    patch: Dict[str, Any] = {"status": status}
    if agent:
        patch.update(
            {
                "agent_id": agent.get("agent_id"),
                "agent_ref_id": agent.get("agent_ref_id"),
                "payer_avpa": agent.get("payer_avpa"),
                "juspay_customer_id": agent.get("customer_id"),
            }
        )
    if action:
        patch.update(
            {
                "action_id": action.get("action_id"),
                "action_ref_id": action.get("action_ref_id"),
                "action_status": str(action.get("status") or "").upper() or None,
            }
        )
        constraints = action.get("intent_constraints")
        if isinstance(constraints, dict):
            # What the rider approved, which may differ from what we proposed.
            patch["intent_constraints"] = constraints
    return patch


# ---- draw admission (pure) ----
def _money(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _constraint_time(value: Any) -> Optional[datetime]:
    """Constraint timestamp: 10-digit epoch-second string (current rows),
    ISO-8601 UTC (2026-09-02) or 13-digit epoch-millisecond string (oldest
    rows). Kept inline — crm must not import from app.services (boundary
    rules)."""
    try:
        text = str(value)
        if text.isdigit():
            divisor = 1000 if len(text) >= 13 else 1
            return datetime.fromtimestamp(int(text) / divisor, tz=timezone.utc)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except (TypeError, ValueError, OverflowError):
        return None


def check_draw(
    agent: CrmCustomerAgent,
    usage: DrawUsage,
    amount: str,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """None when the draw is allowed, else the refusal reason.

    Reasons: action_missing | agent_inactive | action_inactive |
    limit_unverifiable | invalid_amount | over_limit | total_exhausted |
    draws_exhausted | not_yet_valid | expired.
    """
    now = now or datetime.now(timezone.utc)
    if not agent.action_id:
        return "action_missing"
    if agent.status != "ACTIVE":
        return "agent_inactive"
    if (agent.action_status or "").upper() != "ACTIVE":
        return "action_inactive"
    constraints = agent.intent_constraints
    if not isinstance(constraints, dict):
        return "limit_unverifiable"
    draw = _money(amount)
    if draw is None or draw <= 0:
        return "invalid_amount"
    cap = _money(constraints.get("max_per_draw"))
    if cap is None:
        return "limit_unverifiable"
    if draw > cap:
        return "over_limit"
    total_cap = _money(constraints.get("max_total"))
    if total_cap is not None and usage.drawn_total + draw > total_cap:
        return "total_exhausted"
    max_draws = constraints.get("max_draws")
    if max_draws is not None:
        try:
            if usage.draw_count >= int(max_draws):
                return "draws_exhausted"
        except (TypeError, ValueError):
            return "limit_unverifiable"
    valid_from = _constraint_time(constraints.get("valid_from"))
    if valid_from is not None and now < valid_from:
        return "not_yet_valid"
    valid_till = _constraint_time(constraints.get("valid_till"))
    if valid_till is not None and now > valid_till:
        return "expired"
    return None


def remaining(agent: CrmCustomerAgent, usage: DrawUsage) -> Dict[str, Any]:
    """What /status shows: caps and what is left, all decimal strings."""
    constraints = agent.intent_constraints or {}
    cap = _money(constraints.get("max_per_draw"))
    total_cap = _money(constraints.get("max_total"))
    max_draws = constraints.get("max_draws")
    out: Dict[str, Any] = {
        "max_per_draw": str(cap) if cap is not None else None,
        "max_total": str(total_cap) if total_cap is not None else None,
        "max_draws": max_draws,
        "drawn_total": str(usage.drawn_total),
        "draw_count": usage.draw_count,
        "remaining_total": (
            str(max(total_cap - usage.drawn_total, Decimal("0.00")))
            if total_cap is not None
            else None
        ),
        "remaining_draws": None,
    }
    try:
        if max_draws is not None:
            out["remaining_draws"] = max(int(max_draws) - usage.draw_count, 0)
    except (TypeError, ValueError):
        pass
    return out


# ---- gather -> decide -> apply ----
EVENT_SOURCE = "agentic-upi"
TOPIC_AGENT = "agentic.agent"


async def _emit(
    merchant_id: str, customer_id: str, topic: str, key: str, payload: Dict[str, Any]
) -> None:
    try:
        await record_event(
            merchant_id=merchant_id,
            source=EVENT_SOURCE,
            topic=topic,
            external_id=key,
            payload=payload,
            customer_id=customer_id,
        )
    except Exception:  # mirror posture: a lost event never breaks the flow
        logger.opt(exception=True).error(f"agentic: event {topic} {key} not recorded")


async def create_attempt(
    *,
    merchant_id: str,
    customer_id: str,
    juspay_customer_id: Optional[str],
    agent_obj_ref: str,
    action_obj_ref: str,
    intent_constraints: Dict[str, Any],
) -> CrmCustomerAgent:
    row = await accessor.insert_attempt(
        merchant_id=merchant_id,
        customer_id=customer_id,
        juspay_customer_id=juspay_customer_id,
        agent_obj_ref=agent_obj_ref,
        action_obj_ref=action_obj_ref,
        intent_constraints=intent_constraints,
    )
    await _emit(
        merchant_id,
        customer_id,
        TOPIC_AGENT,
        f"{agent_obj_ref}:attempted",
        {
            "agent_obj_ref": agent_obj_ref,
            "status": row.status,
            "proposed": intent_constraints,
        },
    )
    return row


async def patch_attempt(
    agent_obj_ref: str,
    patch: Dict[str, Any],
    clear: Iterable[str] = (),
) -> Optional[CrmCustomerAgent]:
    """``clear`` names columns to set NULL — the only way to drop a value,
    since None in ``patch`` means "leave untouched"."""
    return await accessor.patch_attempt(agent_obj_ref, patch, clear=clear)


async def set_status(agent_obj_ref: str, status: str) -> Optional[CrmCustomerAgent]:
    row = await accessor.get_by_agent_obj_ref(agent_obj_ref)
    updated = await accessor.patch_attempt(agent_obj_ref, {"status": status})
    if row and updated and updated.status != row.status:
        await _emit(
            row.merchant_id,
            row.customer_id,
            TOPIC_AGENT,
            f"{agent_obj_ref}:{updated.status}:{int(datetime.now(timezone.utc).timestamp())}",
            {"agent_obj_ref": agent_obj_ref, "from": row.status, "to": updated.status},
        )
    return updated


async def apply_juspay_records(
    row: CrmCustomerAgent,
    agent: Optional[Dict[str, Any]],
    action: Optional[Dict[str, Any]] = None,
) -> Optional[CrmCustomerAgent]:
    """Fold Juspay's agent record (and, when fetched, the action record —
    the only carrier of the approved limits) into the row.

    ``agent=None`` means Juspay says the onboarding expired. When no
    ``action`` is passed, the one embedded on the agent is used by OUR
    reference only (translate.pick_action — fail closed).
    """
    if action is None and agent is not None:
        action = pick_action(agent, row.action_obj_ref)
    patch = plan_patch(row.status, agent, action)
    patch["verified_at"] = datetime.now(timezone.utc)
    updated = await accessor.patch_attempt(row.agent_obj_ref, patch)
    if updated and updated.status != row.status:
        logger.info(
            f"agentic: {row.agent_obj_ref} {row.status} -> {updated.status} "
            f"(agent={updated.agent_id} action={updated.action_id})"
        )
        await _emit(
            row.merchant_id,
            row.customer_id,
            TOPIC_AGENT,
            f"{row.agent_obj_ref}:{updated.status}:{int(patch['verified_at'].timestamp())}",
            {
                "agent_obj_ref": row.agent_obj_ref,
                "from": row.status,
                "to": updated.status,
                "agent_id": updated.agent_id,
                "action_id": updated.action_id,
                "approved": updated.intent_constraints,
            },
        )
    return updated
