"""Accessor: the UAP side of a chat session — rider binding and the
ticket-payment ledger in ``metadata.uap_draws``. Mechanical DB access only;
what a draw means lives in ``app/services/uap/ledger.py``."""

import json
from typing import Any, Dict, List, Optional, Tuple

from app.core.logger import logger
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.uap_session import (
    agent_usage_query,
    merge_session_template_vars_query,
    settle_session_draw_query,
    upsert_session_draw_query,
)


def _draws(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return [d for d in (value or []) if isinstance(d, dict)]


async def bind_session_customer(
    session_id: str,
    customer_id: str,
    mobile_number: Optional[str],
    rider_token: Optional[str] = None,
) -> bool:
    """Write the customer (and the session's own id) into the session's
    template_vars — plus the rider's NY token when the page has it, which
    the chat tools read as ``{rider_token}``. ``rider_ref`` is also what
    the ledger aggregates a rider's sessions by. False when the session
    does not exist."""
    try:
        patch: Dict[str, Any] = {"rider_ref": customer_id, "session_id": session_id}
        if mobile_number:
            patch["rider_mobile"] = mobile_number
        if rider_token:
            patch["rider_token"] = rider_token
        query, values = merge_session_template_vars_query(session_id, patch)
        rows = await run_parameterized_query(query, values)
        return bool(rows)
    except Exception:
        logger.exception(f"uap: failed to bind customer to session {session_id}")
        raise


async def upsert_session_draw(
    session_id: str, order_id: str, element: Dict[str, Any], patch: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Append or merge one draw; returns the session's draws afterwards
    (empty when the session does not exist)."""
    query, values = upsert_session_draw_query(session_id, order_id, element, patch)
    rows = await run_parameterized_query(query, values)
    return _draws(rows[0]["draws"]) if rows else []


async def settle_session_draw(
    session_id: str, journey_id: str, patch: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Merge what a NY poll learned into the draws for ``journey_id``;
    returns the session's draws afterwards."""
    query, values = settle_session_draw_query(session_id, journey_id, patch)
    rows = await run_parameterized_query(query, values)
    return _draws(rows[0]["draws"]) if rows else []


async def get_agent_usage(
    merchant_id: str, customer_id: str, agent_ref: str
) -> Tuple[str, int]:
    """(drawn_total as a decimal string, draw_count) for one agent across
    every session of the rider."""
    query, values = agent_usage_query(merchant_id, customer_id, agent_ref)
    rows = await run_parameterized_query(query, values)
    if not rows:
        return "0", 0
    return str(rows[0]["drawn_total"] or "0"), int(rows[0]["draw_count"] or 0)
