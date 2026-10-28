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

Two homes for a handle. The canon five are COLUMNS on crm_customer. A
channel's id (a Xyne user id; the kinds in app/crm/shared/handles.py) is a
ROW in crm_customer_handle (085), so the next channel needs no migration.
Rows are probed after the columns and follow the same ladder: an
overwrite RELEASES the old row (kept as history) and inserts the new one.
A staple releases the loser's live rows — they stay on the loser as its
audit + undo copies, as its columns do — and the survivor attaches a copy
of each one whose slot it does not already fill, so every id the person
used still finds them. Every write into a customer's row slots happens
under that customer's row lock, taken after the losers are merged (the
order resolve() has always locked in) and proving the customer is still
active: racing resolves of one person queue there, and none writes onto a
record a concurrent staple has just merged away. An event that carries no
row kind and staples nobody runs exactly the statements it ran before 085;
a staple adds one release per loser.

Also upserts the platform_identity registry rows — best-effort, never
blocking.
"""

import uuid as uuid_module
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional, Tuple

from app.core.logger import logger
from app.crm.identity.db import DbTxn, UniqueViolation, accessor, atomically
from app.crm.identity.db.queries import HANDLE_COLUMNS
from app.crm.platform.contracts import ensure_identities
from app.crm.shared.handles import ROW_KINDS, UNSCOPED, normalize_row_handle
from app.crm.shared.normalize import normalize_email, normalize_phone

# ADR 0021: which evidence classes may replace an occupied handle slot.
EVIDENCE_OVERWRITES = frozenset({"declared", "observed"})
RESOLVE_EVIDENCE = frozenset({"declared", "observed", "imported"})


# A customer's live row handles, (kind, scope) -> value: one per slot.
RowHandles = Dict[Tuple[str, str], str]


class _SurvivorMoved(Exception):
    """The customer this resolve was about to write row handles to was
    merged away by a racing resolve between the probe and the lock.
    Answered like an insert race: the atom rolls back and resolve()
    re-probes, now seeing the merge."""


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


def plan_inherited_rows(
    released: Iterable[Tuple[str, str, str]], survivor_rows: RowHandles
) -> List[Tuple[str, str, str]]:
    """PURE: which of a stapled loser's released ids the survivor attaches —
    each one whose (kind, scope) slot it does not already fill. The
    survivor's own id wins a clash; the loser's stays released on the loser,
    as audit (the columns behave the same way: the survivor keeps its own)."""
    taken = set(survivor_rows)
    inherited: List[Tuple[str, str, str]] = []
    for kind, scope, value in released:
        if (kind, scope) in taken:
            continue
        taken.add((kind, scope))
        inherited.append((kind, scope, value))
    return inherited


def _normalize(handles: Dict[str, str]) -> Dict[str, str]:
    normalized: Dict[str, str] = {}
    for name, value in (handles or {}).items():
        if not value:
            continue
        if name in ROW_KINDS:
            row_value = normalize_row_handle(name, value)
            if row_value is None:
                logger.warning(
                    f"resolve: dropping unusable {name} (len={len(str(value))})"
                )
                continue
            normalized[name] = row_value
            continue
        if name not in HANDLE_COLUMNS:
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
    """GATHER: probe every handle's partial unique, fixed order (the law):
    the columns first, then the row kinds — deduped by owner id. Rows
    behave as mappings (asyncpg.Record)."""
    owners: Dict[Any, Any] = {}
    for column in HANDLE_COLUMNS:
        value = handles.get(column)
        if not value:
            continue
        row = await accessor.probe_customer(txn, merchant_id, column, value)
        if row is not None:
            owners[row["id"]] = row
    for kind in ROW_KINDS:
        value = handles.get(kind)
        if not value:
            continue
        row = await accessor.probe_row_handle(txn, merchant_id, kind, UNSCOPED, value)
        if row is not None:
            owners[row["id"]] = row
    return list(owners.values())


async def _lock_survivor(txn: DbTxn, merchant_id: str, survivor: str) -> RowHandles:
    """The survivor's row lock, proof it is still active, and its live row
    handles — taken before the first write into its row slots. Raises
    _SurvivorMoved when a racing staple merged it away after our probe."""
    if not await accessor.lock_live_customer(txn, merchant_id, survivor):
        raise _SurvivorMoved(survivor)
    return {
        (row["kind"], row["scope"]): row["value"]
        for row in await accessor.live_row_handles(txn, merchant_id, survivor)
    }


async def _apply_resolution(
    txn: DbTxn,
    merchant_id: str,
    plan: ResolutionPlan,
    column_handles: Dict[str, str],
    row_handles: Dict[str, str],
    evidence: str,
) -> uuid_module.UUID:
    """APPLY: execute the plan through accessors — no decisions here.

    Lock order: the losers first (each merge locks one), the survivor last —
    the order resolve() has always locked in, so two staples meeting on the
    same records queue instead of deadlocking. The survivor's lock is taken
    before the first write into its row slots, and only when there is one to
    make: a loser held row handles, or the letter carries a row kind. A
    column-only staple runs the statements it always ran plus one release per
    loser; a letter with no row kind and no staple, exactly the old ones."""
    if plan.create:
        customer_id = await accessor.insert_customer(txn, merchant_id, column_handles)
        await _attach_row_handles(
            txn, merchant_id, str(customer_id), {}, row_handles, evidence
        )
        return customer_id
    survivor = str(plan.survivor_id)
    released: List[Tuple[str, str, str]] = []
    for loser_id in plan.losers:
        await accessor.merge_customer(txn, str(loser_id), survivor)
        released.extend(
            await accessor.release_live_row_handles(txn, merchant_id, str(loser_id))
        )
        logger.info(
            f"resolve: stapled customer {loser_id} into {plan.survivor_id} "
            f"(handle co-occurrence, merchant {merchant_id})"
        )
    rows: RowHandles = {}
    if released or row_handles:
        rows = await _lock_survivor(txn, merchant_id, survivor)
        for kind, scope, value in plan_inherited_rows(released, rows):
            await accessor.insert_row_handle(
                txn, merchant_id, survivor, kind, scope, value
            )
            rows[(kind, scope)] = value
    await accessor.apply_handles(txn, survivor, plan.writes)
    await _attach_row_handles(txn, merchant_id, survivor, rows, row_handles, evidence)
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
    except (UniqueViolation, _SurvivorMoved):
        # Insert race: someone created the row, attached the row handle, or
        # merged our survivor away between our probe and our write. The
        # partial unique (or the survivor lock) refereed — re-probe finds
        # them.
        customer_id = await atomically(
            _resolve_in_txn, merchant_id, normalized, evidence
        )

    await ensure_identities(normalized)  # registry upsert, never raises
    return customer_id


async def _resolve_in_txn(
    txn: DbTxn, merchant_id: str, handles: Dict[str, str], evidence: str
) -> uuid_module.UUID:
    """ATOMIC: probe → staple → attach, columns and rows together — a
    half-done merge must not exist (staple-never-melt; racing resolvers
    converge on one customer)."""
    column_handles = {k: v for k, v in handles.items() if k in HANDLE_COLUMNS}
    row_handles = {k: v for k, v in handles.items() if k in ROW_KINDS}
    owners = await _gather_owners(txn, merchant_id, handles)  # GATHER
    plan = plan_resolution(owners, column_handles, evidence)  # DECIDE (pure)
    return await _apply_resolution(
        txn, merchant_id, plan, column_handles, row_handles, evidence
    )  # APPLY


async def _attach_row_handles(
    txn: DbTxn,
    merchant_id: str,
    customer_id: str,
    rows: RowHandles,
    incoming: Dict[str, str],
    evidence: str,
) -> None:
    """The ADR 0021 ladder over the customer's row handles, inside the same
    atom, against ``rows`` — read under the customer's lock (_lock_survivor),
    or empty for a customer this atom just created. Attach when free,
    overwrite as release + insert, keep on `imported`. Nothing incoming,
    nothing written."""
    current = {
        kind: value for (kind, scope), value in rows.items() if scope == UNSCOPED
    }
    for kind, value in plan_handle_writes(current, incoming, evidence).items():
        if current.get(kind) is not None:
            await accessor.release_row_handle(
                txn, merchant_id, customer_id, kind, UNSCOPED
            )
        await accessor.insert_row_handle(
            txn, merchant_id, customer_id, kind, UNSCOPED, value
        )
