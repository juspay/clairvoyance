"""agentic module — public surface (module rules §1).

The ONLY file other modules and buddy callers (the /uap routes, the voice
tool) may import from app/crm/agentic.
"""

from app.crm.agentic.db.accessor import (
    get_by_agent_id,
    get_by_agent_obj_ref,
    get_drawable_for_customer,
    get_latest_for_customer,
    list_drawable_for_customer,
    set_preferred_agent,
)
from app.crm.agentic.payment_agent import (
    TERMINAL_STATUSES,
    apply_juspay_records,
    check_draw,
    create_attempt,
    derive_status,
    patch_attempt,
    pick_action,
    plan_patch,
    remaining,
    set_status,
)
from app.crm.agentic.schemas import CrmCustomerAgent, DrawUsage

__all__ = [
    "CrmCustomerAgent",
    "DrawUsage",
    "TERMINAL_STATUSES",
    "apply_juspay_records",
    "check_draw",
    "create_attempt",
    "derive_status",
    "get_by_agent_id",
    "get_by_agent_obj_ref",
    "get_drawable_for_customer",
    "get_latest_for_customer",
    "list_drawable_for_customer",
    "patch_attempt",
    "set_preferred_agent",
    "pick_action",
    "plan_patch",
    "remaining",
    "set_status",
]
