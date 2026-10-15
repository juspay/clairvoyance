"""SQL builders for crm_segment_member (T21, canon/05-audiences.md:32-51) —
one table, one file (module rules §1 at scale). $1 placeholders only.

A member row has no merchant_id of its own (a pure child row): writes run
inside the add-members atom AFTER the parent segment was locked under its
merchant, and every read enters through the parent — joined on
``crm_segment.merchant_id = $1``.
"""

from typing import Any, List, Optional, Tuple

from app.crm.outreach.db.queries.tables import SEGMENT_MEMBER_TABLE, SEGMENT_TABLE


def insert_members_query(
    segment_id: str, customer_ids: List[str], source: Optional[str]
) -> Tuple[str, List[Any]]:
    """Every customer in ONE statement. The composite PK is the set, so a
    customer already on the list is a no-op — ``RETURNING`` yields only the
    rows that were really added, which is how the report tells "added" from
    "already a member"."""
    query = f"""
        INSERT INTO {SEGMENT_MEMBER_TABLE} (segment_id, customer_id, source)
        SELECT $1, customer_id, $3
        FROM unnest($2::uuid[]) AS customer_id
        ON CONFLICT (segment_id, customer_id) DO NOTHING
        RETURNING customer_id
    """
    return query, [segment_id, customer_ids, source]


def member_page_query(
    merchant_id: str, segment_id: str, after: Optional[str], limit: int
) -> Tuple[str, List[Any]]:
    """One page of a static list's members, in customer_id order — a keyset
    page the primary key carries, so page 400 costs what page 1 costs and a
    member added mid-read can never shift a page boundary. Entered through
    the parent: another merchant's segment id yields no rows."""
    query = f"""
        SELECT m.customer_id, m.added_at, m.source
        FROM {SEGMENT_MEMBER_TABLE} m
        JOIN {SEGMENT_TABLE} s ON s.id = m.segment_id
        WHERE s.merchant_id = $1
          AND m.segment_id = $2
          AND ($3::uuid IS NULL OR m.customer_id > $3::uuid)
        ORDER BY m.customer_id
        LIMIT $4
    """
    return query, [merchant_id, segment_id, after, limit]


def member_ids_query(
    merchant_id: str, segment_id: str, after: Optional[str], limit: int
) -> Tuple[str, List[Any]]:
    """member_page_query's page with the id only — what a broadcast's freeze
    reads. Same parent, same keyset, same order; no added_at or source to
    decode, because the freeze stores who and nothing else."""
    query = f"""
        SELECT m.customer_id
        FROM {SEGMENT_MEMBER_TABLE} m
        JOIN {SEGMENT_TABLE} s ON s.id = m.segment_id
        WHERE s.merchant_id = $1
          AND m.segment_id = $2
          AND ($3::uuid IS NULL OR m.customer_id > $3::uuid)
        ORDER BY m.customer_id
        LIMIT $4
    """
    return query, [merchant_id, segment_id, after, limit]
