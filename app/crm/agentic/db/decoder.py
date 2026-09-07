"""JSON entry <-> schema translation for the agentic store. DB-side only.

An attempt is one element of ``crm_customer.attributes["agents"]``. The
element carries every CrmCustomerAgent field; datetimes travel as ISO
strings, and merchant/customer ids are re-stamped from the owning row on
the way out so an entry can never claim another tenant.
"""

from typing import Any, Dict

from app.crm.agentic.schemas import CrmCustomerAgent


def decode_agent_entry(
    entry: Dict[str, Any], merchant_id: str, customer_id: str
) -> CrmCustomerAgent:
    data = dict(entry)
    data["merchant_id"] = merchant_id
    data["customer_id"] = customer_id
    data.setdefault("meta", {})
    if not isinstance(data.get("meta"), dict):
        data["meta"] = {}
    return CrmCustomerAgent(**data)
