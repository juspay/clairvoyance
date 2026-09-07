"""Row -> schema translation for the agentic tables. DB-side only."""

import json
from typing import Any

import asyncpg

from app.crm.agentic.schemas import CrmCustomerAgent


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def decode_customer_agent(row: asyncpg.Record) -> CrmCustomerAgent:
    data = dict(row)
    data["id"] = str(data["id"])
    data["customer_id"] = str(data["customer_id"])
    data["intent_constraints"] = _json(data.get("intent_constraints"))
    data["meta"] = _json(data.get("meta")) or {}
    return CrmCustomerAgent(**data)
