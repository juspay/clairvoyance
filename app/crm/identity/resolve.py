"""resolve() — THE only creator of crm_customer rows (canon 07, A6).

BUSINESS LOGIC ONLY, in the house shape: GATHER (accessor reads) →
DECIDE (pure functions returning a plan) → APPLY (accessor writes),
inside a boundary this file owns. DB mechanics live in db/; the driver
never appears here (DbTxn/UniqueViolation come from the db door).

Deterministic, no fuzzy matching: probe every handle's partial unique,
then act on what the probes proved:

- no owner        -> INSERT (insert race -> re-probe; the unique index is
                     the referee — two racing callers converge on one row)
- one owner       -> apply the ADR 0021 handle policy to that row
- several owners  -> the co-occurrence of their handles in ONE trusted
                     payload is STAPLE EVIDENCE: merge (survivor = oldest
                     first_seen_at, tie -> lower id; losers flip to
                     merged_away, their freed handles attach to the
                     survivor). Never an error, never a melt.

Same-row handle changes follow the evidence ladder (ADR 0021):
declared/observed overwrites (the 049 trigger preserves the old value in
the attributes history); imported keeps what live traffic established.
There is NO overwrite flag — callers state what they know (evidence),
identity decides what happens. resolve() refuses `inferred` outright:
identity is never built on guesses.

Also upserts the platform_identity registry rows — best-effort, never
blocking.
"""

import uuid as uuid_module
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple

from app.core.logger import logger
from app.crm.identity.db import DbTxn, UniqueViolation, accessor, atomically
from app.crm.identity.db.queries import HANDLE_COLUMNS
from app.crm.platform.contracts import ensure_identities
from app.crm.shared.normalize import normalize_email, normalize_phone

# ADR 0021: which evidence classes may replace an occupied handle slot.
EVIDENCE_OVERWRITES = frozenset({"declared", "observed"})
RESOLVE_EVIDENCE = frozenset({"declared", "observed", "imported"})


class ResolutionPlan(NamedTuple):
    """The DECIDE output — loggable, testable, executable in one pass."""

    create: bool
    survivor_id: Optional[Any]  # None when create
    losers: Tuple[Any, ...]  # ids to staple into the survivor
    writes: Dict[str, str]  # handle columns to write on the survivor


def plan_handle_writes(
    current: Mapping[str, Optional[str]],
    incoming: Dict[str, str],
    evidence: str,
) -> Dict[str, str]:
    """The ADR 0021 policy table as a pure function.

    current: the row's handle columns as stored. incoming: normalized
    arriving handles. Returns the columns to write: attach when free,
    overwrite when the evidence ladder allows, keep otherwise.
    """
    writes: Dict[str, str] = {}
    for column, value in incoming.items():
        existing = current.get(column)
        if existing is None:
            writes[column] = value
        elif existing == value:
            continue
        elif evidence in EVIDENCE_OVERWRITES:
            writes[column] = value
    return writes


def pick_survivor(owners: List[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Survivor = oldest first_seen_at, tie broken on the lower uuid —
    deterministic, so racing staplers pick the same one."""
    return min(owners, key=lambda r: (r["first_seen_at"], str(r["id"])))


def plan_resolution(
    owners: List[Mapping[str, Any]],
    handles: Dict[str, str],
    evidence: str,
) -> ResolutionPlan:
    """Pure DECIDE: given what the probes proved, say exactly what happens.
    No I/O — takes rows as plain mappings, returns a plan."""
    if not owners:
        return ResolutionPlan(
            create=True, survivor_id=None, losers=(), writes=dict(handles)
        )
    survivor = pick_survivor(owners)
    losers = tuple(r["id"] for r in owners if r["id"] != survivor["id"])
    current = {column: survivor[column] for column in HANDLE_COLUMNS}
    return ResolutionPlan(
        create=False,
        survivor_id=survivor["id"],
        losers=losers,
        writes=plan_handle_writes(current, handles, evidence),
    )


def _normalize(handles: Dict[str, str]) -> Dict[str, str]:
    normalized: Dict[str, str] = {}
    for name, value in (handles or {}).items():
        if name not in HANDLE_COLUMNS or not value:
            continue
        if name == "phone":
            phone = normalize_phone(value)
            if phone is None:
                # never log the raw value — handles are PII
                logger.warning(
                    f"resolve: dropping unparseable phone (len={len(value)})"
                )
                continue
            normalized[name] = phone
        elif name == "email":
            email = normalize_email(value)
            if email is None:
                logger.warning(
                    f"resolve: dropping unparseable email (len={len(value)})"
                )
                continue
            normalized[name] = email
        else:
            stripped = value.strip()
            if stripped:  # whitespace-only handles must not mint customers
                normalized[name] = stripped
    return normalized


async def _gather_owners(
    txn: DbTxn, merchant_id: str, handles: Dict[str, str]
) -> List[Any]:
    """GATHER: probe every handle's partial unique, fixed order (the law),
    deduped by owner id. Rows behave as mappings (asyncpg.Record)."""
    owners: Dict[Any, Any] = {}
    for column in HANDLE_COLUMNS:
        value = handles.get(column)
        if not value:
            continue
        row = await accessor.probe_customer(txn, merchant_id, column, value)
        if row is not None:
            owners[row["id"]] = row
    return list(owners.values())


async def _apply_resolution(
    txn: DbTxn, merchant_id: str, plan: ResolutionPlan, handles: Dict[str, str]
) -> uuid_module.UUID:
    """APPLY: execute the plan through accessors — no decisions here."""
    if plan.create:
        return await accessor.insert_customer(txn, merchant_id, handles)
    for loser_id in plan.losers:
        await accessor.merge_customer(
            txn, merchant_id, str(loser_id), str(plan.survivor_id)
        )
        logger.info(
            f"resolve: stapled customer {loser_id} into {plan.survivor_id} "
            f"(handle co-occurrence, merchant {merchant_id})"
        )
    await accessor.apply_handles(txn, merchant_id, str(plan.survivor_id), plan.writes)
    assert plan.survivor_id is not None  # non-create plans always carry one
    return plan.survivor_id


async def resolve(
    merchant_id: str,
    handles: Dict[str, str],
    *,
    evidence: str = "observed",
    source: str = "unknown",
) -> uuid_module.UUID:
    """resolve(merchant_id, handles{}) -> customer_id.

    ``evidence`` states what kind of claim the caller carries (declared |
    observed | imported — the assert_facts ladder); identity applies the
    ADR 0021 policy, callers never decide overwrites. ``source`` is an
    audit label for logs and future history enrichment.

    Raises ValueError on missing merchant_id, on `inferred` evidence, and
    when no usable handle survives normalization — a customer with zero
    handles is unreachable and must not exist.
    """
    if not merchant_id:
        raise ValueError("resolve() requires a merchant_id")
    if evidence not in RESOLVE_EVIDENCE:
        raise ValueError(
            f"resolve() accepts evidence {sorted(RESOLVE_EVIDENCE)}, "
            f"got {evidence!r} — identity is never built on guesses"
        )
    normalized = _normalize(handles)
    if not normalized:
        raise ValueError("resolve() requires at least one usable handle")

    try:
        customer_id = await atomically(
            _resolve_in_txn, merchant_id, normalized, evidence
        )
    except UniqueViolation:
        # Insert race: someone created the row between our probe and
        # insert. The partial unique refereed — re-probe finds them.
        customer_id = await atomically(
            _resolve_in_txn, merchant_id, normalized, evidence
        )

    await ensure_identities(normalized)  # registry upsert, never raises
    return customer_id


async def _resolve_in_txn(
    txn: DbTxn, merchant_id: str, handles: Dict[str, str], evidence: str
) -> uuid_module.UUID:
    """ATOMIC: probe → staple → attach — a half-done merge must not exist
    (staple-never-melt; racing resolvers converge on one customer)."""
    owners = await _gather_owners(txn, merchant_id, handles)  # GATHER
    plan = plan_resolution(owners, handles, evidence)  # DECIDE (pure)
    return await _apply_resolution(txn, merchant_id, plan, handles)  # APPLY
