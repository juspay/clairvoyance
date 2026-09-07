"""SQL builders for crm_customer_agent.
$1 placeholders only; column names enter SQL only from the tuples below.
"""

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

AGENT_TABLE = "crm_customer_agent"

_AGENT_COLUMNS = """
    id, merchant_id, customer_id, juspay_customer_id,
    agent_obj_ref, action_obj_ref,
    agent_id, agent_ref_id, action_id, action_ref_id, payer_avpa, agentic_app,
    status, action_status, intent_constraints, meta, verified_at,
    created_at, updated_at, preferred
"""

# Columns a patch may carry. Anything else is ignored rather than
# interpolated — the ONLY place column names enter SQL is this tuple.
_PATCHABLE = (
    "juspay_customer_id",
    "action_obj_ref",
    "agent_id",
    "agent_ref_id",
    "action_id",
    "action_ref_id",
    "payer_avpa",
    "agentic_app",
    "status",
    "action_status",
    "intent_constraints",
    "verified_at",
)
_JSON_COLUMNS = {"intent_constraints"}


def insert_attempt_query(
    *,
    merchant_id: str,
    customer_id: str,
    juspay_customer_id: Optional[str],
    agent_obj_ref: str,
    action_obj_ref: str,
    intent_constraints: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """A fresh PENDING attempt. A retry with the same ref returns the
    existing row untouched (the ref is minted per attempt, so a retry
    always carries the same constraints)."""
    query = f"""
        INSERT INTO {AGENT_TABLE} (
            merchant_id, customer_id, juspay_customer_id,
            agent_obj_ref, action_obj_ref, intent_constraints, status
        ) VALUES ($1, $2::uuid, $3, $4, $5, $6::jsonb, 'PENDING')
        ON CONFLICT (agent_obj_ref) DO UPDATE
            SET updated_at = now()
        RETURNING {_AGENT_COLUMNS}
    """
    return query, [
        merchant_id,
        customer_id,
        juspay_customer_id,
        agent_obj_ref,
        action_obj_ref,
        json.dumps(intent_constraints),
    ]


def patch_by_agent_obj_ref_query(
    agent_obj_ref: str, patch: Dict[str, Any], clear: Iterable[str] = ()
) -> Tuple[str, List[Any]]:
    """Merge known fields; absent/None fields untouched. ``clear`` names
    patchable columns to set NULL (the resume path drops the old action
    this way). ``status`` is written whenever present (PENDING→ACTIVE→PAUSED
    are all legitimate overwrites)."""
    sets: List[str] = []
    values: List[Any] = [agent_obj_ref]
    cleared = {c for c in clear if c in _PATCHABLE}
    for col in _PATCHABLE:
        if col in cleared:
            sets.append(f"{col} = NULL")
            continue
        if col not in patch or patch[col] is None:
            continue
        values.append(json.dumps(patch[col]) if col in _JSON_COLUMNS else patch[col])
        n = len(values)
        cast = "::jsonb" if col in _JSON_COLUMNS else ""
        sets.append(f"{col} = ${n}{cast}")
    if not sets:
        return (
            f"SELECT {_AGENT_COLUMNS} FROM {AGENT_TABLE} WHERE agent_obj_ref = $1",
            values,
        )
    sets.append("updated_at = now()")
    query = f"""
        UPDATE {AGENT_TABLE}
           SET {", ".join(sets)}
         WHERE agent_obj_ref = $1
        RETURNING {_AGENT_COLUMNS}
    """
    return query, values


def get_by_agent_obj_ref_query(agent_obj_ref: str) -> Tuple[str, List[Any]]:
    """Webhook / poll lookup — the ref is ours and minted unique per
    attempt, so no tenant is known yet; newest wins if it ever collides."""
    query = f"""
        SELECT {_AGENT_COLUMNS} FROM {AGENT_TABLE}
         WHERE agent_obj_ref = $1
         ORDER BY created_at DESC LIMIT 1
    """
    return query, [agent_obj_ref]


def get_by_agent_id_query(agent_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_AGENT_COLUMNS} FROM {AGENT_TABLE}
         WHERE agent_id = $1
         ORDER BY created_at DESC LIMIT 1
    """
    return query, [agent_id]


def get_latest_for_customer_query(
    merchant_id: str, customer_id: str
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_AGENT_COLUMNS} FROM {AGENT_TABLE}
         WHERE merchant_id = $1 AND customer_id = $2::uuid
         ORDER BY created_at DESC LIMIT 1
    """
    return query, [merchant_id, customer_id]


def get_drawable_for_customer_query(
    merchant_id: str, customer_id: str
) -> Tuple[str, List[Any]]:
    """The newest attempt that can be charged — ACTIVE agent, ACTIVE
    action, all three identifiers — enforced in SQL so the booking path
    cannot trust a half-assembled row."""
    query = f"""
        SELECT {_AGENT_COLUMNS} FROM {AGENT_TABLE}
         WHERE merchant_id = $1 AND customer_id = $2::uuid
           AND status = 'ACTIVE'
           AND action_status = 'ACTIVE'
           AND agent_id IS NOT NULL
           AND action_id IS NOT NULL
           AND payer_avpa IS NOT NULL
         ORDER BY preferred DESC, created_at DESC LIMIT 1
    """
    return query, [merchant_id, customer_id]


def list_drawable_for_customer_query(
    merchant_id: str, customer_id: str
) -> Tuple[str, List[Any]]:
    """Every attempt that can be charged, preferred first — what the
    payment-agent sheet lists and lets the rider pick from."""
    query = f"""
        SELECT {_AGENT_COLUMNS} FROM {AGENT_TABLE}
         WHERE merchant_id = $1 AND customer_id = $2::uuid
           AND status = 'ACTIVE'
           AND action_status = 'ACTIVE'
           AND agent_id IS NOT NULL
           AND action_id IS NOT NULL
           AND payer_avpa IS NOT NULL
         ORDER BY preferred DESC, created_at DESC
    """
    return query, [merchant_id, customer_id]


def set_preferred_agent_query(
    merchant_id: str, customer_id: str, agent_obj_ref: str
) -> Tuple[str, List[Any]]:
    """One statement flips the flag on for the chosen attempt and off for
    every other row of the same customer, so the partial unique index can
    never be violated mid-way."""
    query = f"""
        UPDATE {AGENT_TABLE}
           SET preferred = (agent_obj_ref = $3), updated_at = now()
         WHERE merchant_id = $1 AND customer_id = $2::uuid
        RETURNING agent_obj_ref, preferred
    """
    return query, [merchant_id, customer_id, agent_obj_ref]
