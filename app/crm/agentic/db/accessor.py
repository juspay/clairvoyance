"""agentic accessor — the attempts store (module rules §1).

Attempts live under ``crm_customer.attributes["agents"]``, one JSON element
per onboarding attempt, several per rider. identity owns that row, so every
read and write goes through identity's contracts; this file holds no SQL
and no handle. Every mutation is a pure function over the list, applied by
identity under the customer's row lock, so a poll tick and the SDK callback
patching the same attempt can never lose each other's fields.

Same eight functions, same signatures, as the table-backed version — the
logic files and the /uap routes did not move.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.crm.agentic.db.decoder import decode_agent_entry
from app.crm.agentic.schemas import CrmCustomerAgent
from app.crm.identity.contracts import (
    find_customer_by_attribute,
    mutate_customer_attribute,
    read_customer_attribute,
)

AGENTS_KEY = "agents"

# Fields a patch may carry. Anything else is ignored rather than stored —
# the ONLY place caller keys enter the document is this tuple.
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

# Attempts that still matter to a rider (drawable, in flight, or paused)
# are always kept; of the rest only the newest KEEP_SETTLED stay, so a
# rider who retried onboarding fifty times does not carry fifty corpses.
_LIVE_STATUSES = {"PENDING", "ACTIVE", "PAUSED"}
KEEP_SETTLED = 20

Entries = List[Dict[str, Any]]


# ---- pure helpers over the list (unit-tested without a database) ----


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entries(value: Any) -> Entries:
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def _created(entry: Dict[str, Any]) -> str:
    return str(entry.get("created_at") or "")


def newest_first(entries: Entries) -> Entries:
    return sorted(entries, key=_created, reverse=True)


def _is_drawable(entry: Dict[str, Any]) -> bool:
    """ACTIVE agent, ACTIVE action, all three identifiers — the same test
    the booking path used to get from SQL, so it cannot trust a
    half-assembled attempt."""
    return (
        entry.get("status") == "ACTIVE"
        and entry.get("action_status") == "ACTIVE"
        and bool(entry.get("agent_id"))
        and bool(entry.get("action_id"))
        and bool(entry.get("payer_avpa"))
    )


def drawable_entries(entries: Entries) -> Entries:
    """Preferred first, then newest — what the booking path charges and
    what the payment-agent sheet lists."""
    return sorted(
        (e for e in entries if _is_drawable(e)),
        key=lambda e: (1 if e.get("preferred") else 0, _created(e)),
        reverse=True,
    )


def find_by_ref(entries: Entries, agent_obj_ref: str) -> Optional[Dict[str, Any]]:
    hits = [e for e in entries if e.get("agent_obj_ref") == agent_obj_ref]
    return newest_first(hits)[0] if hits else None


def find_by_agent_id(entries: Entries, agent_id: str) -> Optional[Dict[str, Any]]:
    hits = [e for e in entries if e.get("agent_id") == agent_id]
    return newest_first(hits)[0] if hits else None


def prune(entries: Entries) -> Entries:
    """Keep every live or preferred attempt and the newest KEEP_SETTLED of
    the rest, oldest dropped first. Order is preserved."""
    settled = [
        e
        for e in entries
        if e.get("status") not in _LIVE_STATUSES and not e.get("preferred")
    ]
    drop = {id(e) for e in newest_first(settled)[KEEP_SETTLED:]}
    return [e for e in entries if id(e) not in drop]


def upsert_attempt(
    entries: Entries,
    *,
    juspay_customer_id: Optional[str],
    agent_obj_ref: str,
    action_obj_ref: str,
    intent_constraints: Dict[str, Any],
) -> Tuple[Entries, Dict[str, Any]]:
    """A fresh PENDING attempt. A retry with the same ref returns the
    existing entry untouched (the ref is minted per attempt, so a retry
    always carries the same constraints)."""
    existing = find_by_ref(entries, agent_obj_ref)
    if existing is not None:
        existing["updated_at"] = _now()
        return entries, existing
    now = _now()
    entry: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "juspay_customer_id": juspay_customer_id,
        "agent_obj_ref": agent_obj_ref,
        "action_obj_ref": action_obj_ref,
        "agent_id": None,
        "agent_ref_id": None,
        "action_id": None,
        "action_ref_id": None,
        "payer_avpa": None,
        "agentic_app": None,
        "status": "PENDING",
        "action_status": None,
        "intent_constraints": intent_constraints,
        "meta": {},
        "verified_at": None,
        "created_at": now,
        "updated_at": now,
        "preferred": False,
    }
    return prune(entries + [entry]), entry


def apply_patch(
    entries: Entries,
    agent_obj_ref: str,
    patch: Dict[str, Any],
    clear: Iterable[str] = (),
) -> Tuple[Entries, Optional[Dict[str, Any]]]:
    """Merge known fields onto the attempt with this ref; absent/None
    fields untouched. ``clear`` names patchable fields to blank (the
    resume path drops the old action this way). ``status`` is written
    whenever present (PENDING->ACTIVE->PAUSED are all legitimate)."""
    entry = find_by_ref(entries, agent_obj_ref)
    if entry is None:
        return entries, None
    cleared = {c for c in clear if c in _PATCHABLE}
    for field in _PATCHABLE:
        if field in cleared:
            entry[field] = None
            continue
        if field not in patch or patch[field] is None:
            continue
        value = patch[field]
        entry[field] = value.isoformat() if isinstance(value, datetime) else value
    entry["updated_at"] = _now()
    return entries, entry


def choose_preferred(entries: Entries, agent_obj_ref: str) -> Tuple[Entries, bool]:
    """Flip the flag on for the chosen attempt and off for every other one,
    so at most one attempt per rider is ever preferred."""
    matched = False
    for e in entries:
        hit = e.get("agent_obj_ref") == agent_obj_ref
        matched = matched or hit
        if bool(e.get("preferred")) != hit:
            e["preferred"] = hit
            e["updated_at"] = _now()
    return entries, matched


def customer_id_from_ref(agent_obj_ref: str) -> Optional[str]:
    """Our refs are ``agent_<crm customer uuid>_<stamp>`` — the customer is
    in the name, so the poll and the webhook need no scan. Anything else
    (an adopted Juspay ref that does not follow it) falls back to a scan."""
    if not agent_obj_ref.startswith("agent_"):
        return None
    body = agent_obj_ref[len("agent_") :]
    candidate, sep, _stamp = body.rpartition("_")
    if not sep:
        return None
    try:
        return str(uuid.UUID(candidate))
    except ValueError:
        return None


# ---- the eight accessors ----


def _decode(
    entry: Dict[str, Any], merchant_id: str, customer_id: str
) -> CrmCustomerAgent:
    return decode_agent_entry(entry, merchant_id, customer_id)


async def _locate(agent_obj_ref: str) -> Optional[Tuple[str, str, Entries]]:
    """(customer_id, merchant_id, entries) for the customer owning this ref."""
    customer_id = customer_id_from_ref(agent_obj_ref)
    if customer_id:
        hit = await read_customer_attribute(customer_id, AGENTS_KEY)
        if hit is not None and find_by_ref(_entries(hit[2]), agent_obj_ref) is not None:
            return hit[0], hit[1], _entries(hit[2])
    hit = await find_customer_by_attribute(
        AGENTS_KEY, [{"agent_obj_ref": agent_obj_ref}]
    )
    if hit is None:
        return None
    return hit[0], hit[1], _entries(hit[2])


async def insert_attempt(
    *,
    merchant_id: str,
    customer_id: str,
    juspay_customer_id: Optional[str],
    agent_obj_ref: str,
    action_obj_ref: str,
    intent_constraints: Dict[str, Any],
) -> CrmCustomerAgent:
    def mutation(current: Any) -> Tuple[Any, Dict[str, Any]]:
        return upsert_attempt(
            _entries(current),
            juspay_customer_id=juspay_customer_id,
            agent_obj_ref=agent_obj_ref,
            action_obj_ref=action_obj_ref,
            intent_constraints=intent_constraints,
        )

    result = await mutate_customer_attribute(customer_id, AGENTS_KEY, mutation)
    if result is None:
        raise RuntimeError(f"agentic: customer {customer_id} not found for attempt")
    owner_merchant, entry = result
    if owner_merchant != merchant_id:
        # tenant-pinned like the old FK: a row can never point at another
        # merchant's customer
        raise RuntimeError("agentic: customer belongs to another merchant")
    return _decode(entry, owner_merchant, customer_id)


async def patch_attempt(
    agent_obj_ref: str, patch: Dict[str, Any], clear: Iterable[str] = ()
) -> Optional[CrmCustomerAgent]:
    located = await _locate(agent_obj_ref)
    if located is None:
        return None
    customer_id, _merchant, _ = located

    def mutation(current: Any) -> Tuple[Any, Optional[Dict[str, Any]]]:
        return apply_patch(_entries(current), agent_obj_ref, patch, clear=clear)

    result = await mutate_customer_attribute(customer_id, AGENTS_KEY, mutation)
    if result is None or result[1] is None:
        return None
    return _decode(result[1], result[0], customer_id)


async def get_by_agent_obj_ref(agent_obj_ref: str) -> Optional[CrmCustomerAgent]:
    located = await _locate(agent_obj_ref)
    if located is None:
        return None
    customer_id, merchant_id, entries = located
    entry = find_by_ref(entries, agent_obj_ref)
    return _decode(entry, merchant_id, customer_id) if entry else None


async def get_by_agent_id(agent_id: str) -> Optional[CrmCustomerAgent]:
    hit = await find_customer_by_attribute(AGENTS_KEY, [{"agent_id": agent_id}])
    if hit is None:
        return None
    entry = find_by_agent_id(_entries(hit[2]), agent_id)
    return _decode(entry, hit[1], hit[0]) if entry else None


async def _customer_entries(merchant_id: str, customer_id: str) -> Entries:
    hit = await read_customer_attribute(customer_id, AGENTS_KEY)
    if hit is None or hit[1] != merchant_id:
        return []
    return _entries(hit[2])


async def get_latest_for_customer(
    merchant_id: str, customer_id: str
) -> Optional[CrmCustomerAgent]:
    entries = newest_first(await _customer_entries(merchant_id, customer_id))
    return _decode(entries[0], merchant_id, customer_id) if entries else None


async def get_drawable_for_customer(
    merchant_id: str, customer_id: str
) -> Optional[CrmCustomerAgent]:
    entries = drawable_entries(await _customer_entries(merchant_id, customer_id))
    return _decode(entries[0], merchant_id, customer_id) if entries else None


async def list_drawable_for_customer(
    merchant_id: str, customer_id: str
) -> List[CrmCustomerAgent]:
    entries = drawable_entries(await _customer_entries(merchant_id, customer_id))
    return [_decode(e, merchant_id, customer_id) for e in entries]


async def set_preferred_agent(
    merchant_id: str, customer_id: str, agent_obj_ref: str
) -> bool:
    """True when the chosen ref belonged to this customer (and is now
    preferred); False when nothing matched."""

    def mutation(current: Any) -> Tuple[Any, bool]:
        return choose_preferred(_entries(current), agent_obj_ref)

    result = await mutate_customer_attribute(customer_id, AGENTS_KEY, mutation)
    return bool(result and result[0] == merchant_id and result[1])


__all__ = [
    "insert_attempt",
    "patch_attempt",
    "get_by_agent_obj_ref",
    "get_by_agent_id",
    "get_latest_for_customer",
    "get_drawable_for_customer",
    "list_drawable_for_customer",
    "set_preferred_agent",
]
