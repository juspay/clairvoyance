"""SQL builders for crm_segment (T15, canon/05-audiences.md) — one table, one
file (module rules §1 at scale). $1 placeholders only — every value
parameterized; merchant_id is $1 on every read and write (tenancy law).
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from app.crm.outreach.db.queries.tables import SEGMENT_MEMBER_TABLE, SEGMENT_TABLE

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


def insert_dynamic_segment_query(
    merchant_id: str, name: str, definition: Dict[str, Any], created_by: Optional[str]
) -> Tuple[str, List[Any]]:
    """A dynamic segment: the stored question, checked before it got here.
    No count yet — member_count / computed_at stay NULL until the rule is
    first counted (phase 4); a NULL stamp says "never counted", not zero."""
    query = f"""
        INSERT INTO {SEGMENT_TABLE} (merchant_id, name, definition, created_by)
        VALUES ($1, $2, $3::jsonb, $4)
        RETURNING {_SEGMENT_COLUMNS}
    """
    return query, [merchant_id, name, json.dumps(definition), created_by]


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


def lock_segment_query(merchant_id: str, segment_id: str) -> Tuple[str, List[Any]]:
    """Inside the add-members atom: take the segment row FOR UPDATE before any
    member is written, so two concurrent adds to one list run one after the
    other — the second one's count then sees the first one's rows, and the
    stamped count stays exact. Returns the row only when it is this
    merchant's; the caller reads ``is_static`` to refuse a dynamic segment."""
    query = f"""
        SELECT definition IS NULL AS is_static
        FROM {SEGMENT_TABLE}
        WHERE merchant_id = $1 AND id = $2
        FOR UPDATE
    """
    return query, [merchant_id, segment_id]


def stamp_member_count_query(
    merchant_id: str, segment_id: str
) -> Tuple[str, List[Any]]:
    """A static list's count, recounted from its rows and stamped now (T15
    cols 7-8 — exact forever for a list). Recounted, never incremented: a
    count derived from the rows cannot drift from them."""
    query = f"""
        UPDATE {SEGMENT_TABLE}
        SET member_count = (
                SELECT count(*)::int
                FROM {SEGMENT_MEMBER_TABLE}
                WHERE segment_id = $2
            ),
            computed_at = now()
        WHERE merchant_id = $1 AND id = $2
        RETURNING member_count, computed_at
    """
    return query, [merchant_id, segment_id]
