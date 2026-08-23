"""SQL builders for crm_customer (T05). $1 placeholders only.

Handle column names come exclusively from HANDLE_COLUMNS — never from
caller input — so the f-string column interpolation below is safe.
apply_handles_query is THE ONLY builder that writes handle columns
(ADR 0021 lock #3); a second one is a review failure.
"""

from typing import Any, Dict, List, Tuple

CRM_CUSTOMER_TABLE = "crm_customer"

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
    display-name ILIKE (seq scan — acceptable at pilot volume, pg_trgm
    is the follow-up when lists grow)."""
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
