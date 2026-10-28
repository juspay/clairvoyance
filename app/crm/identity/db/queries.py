"""SQL builders for crm_customer (T05) and crm_customer_handle (085).
$1 placeholders only.

Handle column names come exclusively from HANDLE_COLUMNS — never from
caller input — so the f-string column interpolation below is safe.
apply_handles_query is THE ONLY builder that writes handle columns
(ADR 0021 lock #3); a second one is a review failure. The row handles have
their own writers below — insert, release, and a staple's release —
and nothing else writes crm_customer_handle.
"""

from typing import Any, Dict, List, Tuple

CRM_CUSTOMER_TABLE = "crm_customer"
CRM_CUSTOMER_HANDLE_TABLE = "crm_customer_handle"

# The probe order IS the law (canon 01): fixed, deterministic, no fuzz.
HANDLE_COLUMNS = ("phone", "email", "igsid", "shopify_customer_id", "external_ref")

_SUMMARY_COLUMNS = """
    id, merchant_id, display_name, primary_locale, timezone,
    phone, email, igsid, shopify_customer_id, external_ref,
    status, merged_into_id, merged_at, first_seen_at, last_seen_at,
    created_at, updated_at
"""

# Detail adds the attributes jsonb — the list never fetches it.
_CUSTOMER_COLUMNS = _SUMMARY_COLUMNS + ", attributes"


def probe_customer_query(
    merchant_id: str, handle_column: str, value: str
) -> Tuple[str, List[Any]]:
    """Probe one partial unique; returns the row's handles + first_seen_at
    so resolve can plan writes and pick a merge survivor without a
    second read."""
    assert handle_column in HANDLE_COLUMNS
    query = f"""
        SELECT id, first_seen_at, {", ".join(HANDLE_COLUMNS)}
        FROM {CRM_CUSTOMER_TABLE}
        WHERE merchant_id = $1 AND {handle_column} = $2 AND status = 'active'
    """
    return query, [merchant_id, value]


def insert_customer_query(
    merchant_id: str, handles: Dict[str, str]
) -> Tuple[str, List[Any]]:
    columns = ["merchant_id"]
    values: List[Any] = [merchant_id]
    for column in HANDLE_COLUMNS:
        if handles.get(column):
            columns.append(column)
            values.append(handles[column])
    placeholders = ", ".join(f"${i + 1}" for i in range(len(values)))
    query = f"""
        INSERT INTO {CRM_CUSTOMER_TABLE} ({", ".join(columns)})
        VALUES ({placeholders})
        RETURNING id
    """
    return query, values


def apply_handles_query(
    customer_id: str, writes: Dict[str, str]
) -> Tuple[str, List[Any]]:
    """Write planned handle changes (attach + ladder overwrites — the
    accessor decides WHAT, this builder only decides HOW) and bump
    last_seen_at. The 049 history trigger preserves any replaced value;
    updated_at is touched by trigger."""
    sets = ["last_seen_at = now()"]
    values: List[Any] = [customer_id]
    for column in HANDLE_COLUMNS:
        if column in writes:
            values.append(writes[column])
            sets.append(f"{column} = ${len(values)}")
    query = f"""
        UPDATE {CRM_CUSTOMER_TABLE}
        SET {", ".join(sets)}
        WHERE id = $1
    """
    return query, values


def merge_customer_query(loser_id: str, survivor_id: str) -> Tuple[str, List[Any]]:
    """The staple (never melt): one UPDATE on the younger row. The WHERE
    status='active' makes racing staplers converge — the second is a
    no-op. Freed partial uniques let the survivor attach the handles."""
    query = f"""
        UPDATE {CRM_CUSTOMER_TABLE}
        SET status = 'merged_away', merged_into_id = $2, merged_at = now()
        WHERE id = $1 AND status = 'active'
    """
    return query, [loser_id, survivor_id]


# ---- crm_customer_handle: the ids that are ROWS (085) ------------------------
# Kinds and their normal forms live in app/crm/shared/handles.py; every value
# here arrives through resolve(), already normalized. A release stamps
# clock_timestamp(), not now(): a row another transaction inserted is only
# visible here once that transaction committed, so the wall clock is the
# time that can never be earlier than the row's created_at (the table CHECK).


def probe_row_handle_query(
    merchant_id: str, kind: str, scope: str, value: str
) -> Tuple[str, List[Any]]:
    """The ACTIVE customer holding this live row handle, in the shape
    probe_customer_query returns (id, first_seen_at, the handle columns) —
    so resolve gathers an owner found either way identically."""
    columns = ", ".join(f"c.{column}" for column in HANDLE_COLUMNS)
    query = f"""
        SELECT c.id, c.first_seen_at, {columns}
        FROM {CRM_CUSTOMER_HANDLE_TABLE} h
        JOIN {CRM_CUSTOMER_TABLE} c
          ON c.merchant_id = h.merchant_id AND c.id = h.customer_id
        WHERE h.merchant_id = $1 AND h.kind = $2 AND h.scope = $3
          AND h.value = $4 AND h.released_at IS NULL
          AND c.status = 'active'
    """
    return query, [merchant_id, kind, scope, value]


def live_row_handles_query(merchant_id: str, customer_id: str) -> Tuple[str, List[Any]]:
    """The customer's live row handles — at most one per (kind, scope), by
    the slot index."""
    query = f"""
        SELECT kind, scope, value
        FROM {CRM_CUSTOMER_HANDLE_TABLE}
        WHERE merchant_id = $1 AND customer_id = $2 AND released_at IS NULL
    """
    return query, [merchant_id, customer_id]


def insert_row_handle_query(
    merchant_id: str, customer_id: str, kind: str, scope: str, value: str
) -> Tuple[str, List[Any]]:
    """Attach a live row handle. Both partial uniques referee it: an id
    another customer holds, or a second live id in this customer's slot, is
    a UniqueViolation — resolve's re-probe answers it."""
    query = f"""
        INSERT INTO {CRM_CUSTOMER_HANDLE_TABLE}
            (merchant_id, customer_id, kind, scope, value)
        VALUES ($1, $2, $3, $4, $5)
    """
    return query, [merchant_id, customer_id, kind, scope, value]


def release_row_handle_query(
    merchant_id: str, customer_id: str, kind: str, scope: str
) -> Tuple[str, List[Any]]:
    """Release the customer's live id in one slot: it stays as history and
    stops blocking anyone. The ADR 0021 overwrite, done as a row."""
    query = f"""
        UPDATE {CRM_CUSTOMER_HANDLE_TABLE}
        SET released_at = clock_timestamp()
        WHERE merchant_id = $1 AND customer_id = $2 AND kind = $3 AND scope = $4
          AND released_at IS NULL
    """
    return query, [merchant_id, customer_id, kind, scope]


def release_live_row_handles_query(
    merchant_id: str, customer_id: str
) -> Tuple[str, List[Any]]:
    """A staple: every live handle of the loser is released — the loser
    keeps its copies as audit + undo (modules/02-identity.md), the rows
    still pointing at it — and the released ids come back so the survivor
    can attach its own copies."""
    query = f"""
        UPDATE {CRM_CUSTOMER_HANDLE_TABLE}
        SET released_at = clock_timestamp()
        WHERE merchant_id = $1 AND customer_id = $2 AND released_at IS NULL
        RETURNING kind, scope, value
    """
    return query, [merchant_id, customer_id]


def lock_live_customer_query(
    merchant_id: str, customer_id: str
) -> Tuple[str, List[Any]]:
    """Take the survivor's row lock BEFORE any row handle is written, and
    prove it is still active. A resolve that touches row handles waits here
    for a racing one to commit, so it never reads stale rows, never writes
    onto a record a concurrent staple just merged away, and never takes the
    survivor's slot ahead of its lock (the order a deadlock needs).

    FOR NO KEY UPDATE: the same lock the UPDATE in apply_handles_query takes
    today, taken earlier — so it adds no new conflict (a foreign-key check
    from another table still passes), it only moves the wait to before the
    first row write."""
    query = f"""
        SELECT id FROM {CRM_CUSTOMER_TABLE}
        WHERE merchant_id = $1 AND id = $2 AND status = 'active'
        FOR NO KEY UPDATE
    """
    return query, [merchant_id, customer_id]


def get_customer_query(merchant_id: str, customer_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_CUSTOMER_COLUMNS}
        FROM {CRM_CUSTOMER_TABLE}
        WHERE merchant_id = $1 AND id = $2
    """
    return query, [merchant_id, customer_id]


def list_customers_query(
    merchant_id: str, exact_term: str, pattern_term: str, limit: int, offset: int
) -> Tuple[str, List[Any]]:
    """exact_term arrives NORMALIZED by the accessor (E.164 / lowercased)
    so it actually matches the stored form; pattern_term feeds the
    display-name ILIKE, which migration 073's trigram index can serve —
    but only if the planner picks a BitmapOr across all three OR arms,
    which is unmeasured. Assume the seq scan until an EXPLAIN says otherwise."""
    values: List[Any] = [merchant_id]
    where = "merchant_id = $1 AND status = 'active'"
    if exact_term:
        values.append(exact_term)
        values.append(pattern_term)
        where += " AND (phone = $2 OR email = $2 OR display_name ILIKE $3)"
    values.extend([limit, offset])
    query = f"""
        SELECT {_SUMMARY_COLUMNS}
        FROM {CRM_CUSTOMER_TABLE}
        WHERE {where}
        ORDER BY last_seen_at DESC
        LIMIT ${len(values) - 1} OFFSET ${len(values)}
    """
    return query, values


def select_attributes_for_update_query(
    merchant_id: str, customer_id: str
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT attributes FROM {CRM_CUSTOMER_TABLE}
        WHERE merchant_id = $1 AND id = $2
        FOR UPDATE
    """
    return query, [merchant_id, customer_id]


def update_attributes_query(
    merchant_id: str,
    customer_id: str,
    attributes: str,
    materialized: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """Write the assertion history + any materialized winner columns.
    materialized keys come from facts.MATERIALIZED_COLUMNS only."""
    sets = ["attributes = $3::jsonb"]
    values: List[Any] = [merchant_id, customer_id, attributes]
    for column, value in materialized.items():
        assert column in ("display_name", "primary_locale", "timezone")
        values.append(value)
        sets.append(f"{column} = ${len(values)}")
    query = f"""
        UPDATE {CRM_CUSTOMER_TABLE}
        SET {", ".join(sets)}
        WHERE merchant_id = $1 AND id = $2
    """
    return query, values


# ---- attributes as a keyed store (used by other modules via contracts) ----
# Other modules may keep per-customer state under ONE key of ``attributes``
# (agentic keeps its onboarding attempts under "agents"). They never touch
# crm_customer themselves: these builders + identity.attributes are the door.


def select_attributes_by_id_query(customer_id: str) -> Tuple[str, List[Any]]:
    """Merchant-less read: callers holding only a customer id (the poll and
    webhook paths carry our per-attempt refs, which embed it)."""
    query = f"""
        SELECT id, merchant_id, attributes FROM {CRM_CUSTOMER_TABLE}
        WHERE id = $1::uuid
    """
    return query, [customer_id]


def select_attributes_for_update_by_id_query(
    customer_id: str,
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT id, merchant_id, attributes FROM {CRM_CUSTOMER_TABLE}
        WHERE id = $1::uuid
        FOR UPDATE
    """
    return query, [customer_id]


def find_customer_by_attribute_query(key: str, fragment: str) -> Tuple[str, List[Any]]:
    """The customer whose ``attributes -> key`` contains ``fragment`` (jsonb
    ``@>``; for an array key, "an element with these fields"). For the rare
    lookups that carry no customer id (a Juspay agent_id).

    Written as containment on the WHOLE document — ``attributes @>
    {key: fragment}`` — so the jsonb_path_ops GIN index on attributes (added with the agentic module)
    serves it; ``attributes -> key @> fragment`` would be a table scan."""
    query = f"""
        SELECT id, merchant_id, attributes FROM {CRM_CUSTOMER_TABLE}
        WHERE attributes @> jsonb_build_object($1::text, $2::jsonb)
        ORDER BY updated_at DESC
        LIMIT 1
    """
    return query, [key, fragment]
