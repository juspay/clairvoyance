"""SQL builders for crm_segment (T15, canon/05-audiences.md) — one table, one
file (module rules §1 at scale). $1 placeholders only — every value
parameterized; merchant_id is $1 on every read and write (tenancy law).
"""

from typing import Any, List, Optional, Tuple

from app.crm.outreach.db.queries.tables import SEGMENT_TABLE

_SEGMENT_COLUMNS = """
    id, merchant_id, name, definition, member_count, computed_at,
    created_by, created_at, updated_at
"""


def insert_static_list_query(
    merchant_id: str, name: str, created_by: Optional[str]
) -> Tuple[str, List[Any]]:
    """A static list is born empty, and its count is exact from the first
    moment: zero members, stamped now (T15 col 7 — exact forever for a
    list). ``definition`` stays NULL — that NULL is what makes it a list.
    No ON CONFLICT: a second list with the same name is the unique index's
    refusal to surface, never a merge."""
    query = f"""
        INSERT INTO {SEGMENT_TABLE}
            (merchant_id, name, member_count, computed_at, created_by)
        VALUES ($1, $2, 0, now(), $3)
        RETURNING {_SEGMENT_COLUMNS}
    """
    return query, [merchant_id, name, created_by]


def get_segment_query(merchant_id: str, segment_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_SEGMENT_COLUMNS}
        FROM {SEGMENT_TABLE}
        WHERE merchant_id = $1 AND id = $2
    """
    return query, [merchant_id, segment_id]


def list_segments_query(
    merchant_id: str, limit: int, offset: int
) -> Tuple[str, List[Any]]:
    """Newest first — the order crm_segment_merchant_created_ix carries."""
    query = f"""
        SELECT {_SEGMENT_COLUMNS}
        FROM {SEGMENT_TABLE}
        WHERE merchant_id = $1
        ORDER BY created_at DESC, id DESC
        LIMIT $2 OFFSET $3
    """
    return query, [merchant_id, limit, offset]


def count_segments_query(merchant_id: str) -> Tuple[str, List[Any]]:
    """The merchant's segment count, for the list's pager (in the body —
    the RunPage ruling)."""
    query = f"""
        SELECT count(*)::int AS total
        FROM {SEGMENT_TABLE}
        WHERE merchant_id = $1
    """
    return query, [merchant_id]
