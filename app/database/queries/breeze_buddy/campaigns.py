"""SQL builders for campaigns (parameterized; executed via
run_parameterized_query)."""

from typing import Any, List, Optional, Tuple

CAMPAIGN_COLUMNS = (
    "id, reseller_id, merchant_id, template_id, name, status, total_leads, "
    "created_by, created_at, updated_at"
)


def insert_campaign_query(
    reseller_id: str,
    merchant_id: Optional[str],
    template_id: str,
    name: str,
    created_by: Optional[str],
) -> Tuple[str, List[Any]]:
    return (
        f"""
        INSERT INTO campaign (reseller_id, merchant_id, template_id, name, created_by)
        VALUES ($1, $2, $3::UUID, $4, $5)
        RETURNING {CAMPAIGN_COLUMNS}
        """,
        [reseller_id, merchant_id, template_id, name, created_by],
    )


def get_campaign_by_id_query(campaign_id: str) -> Tuple[str, List[Any]]:
    return (
        f"SELECT {CAMPAIGN_COLUMNS} FROM campaign WHERE id = $1::UUID",
        [campaign_id],
    )


def list_campaigns_query(
    reseller_ids: Optional[List[str]],
    merchant_ids: Optional[List[str]],
    page: int,
    limit: int,
) -> Tuple[str, List[Any]]:
    """List campaigns newest-first, optionally scoped to reseller/merchant
    sets (RBAC narrowing happens in the handler)."""
    conditions: List[str] = []
    values: List[Any] = []
    if reseller_ids:
        values.append(reseller_ids)
        conditions.append(f"reseller_id = ANY(${len(values)})")
    if merchant_ids:
        values.append(merchant_ids)
        conditions.append(f"merchant_id = ANY(${len(values)})")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    values.append(limit)
    limit_idx = len(values)
    values.append((page - 1) * limit)
    offset_idx = len(values)
    return (
        f"""
        SELECT {CAMPAIGN_COLUMNS}, COUNT(*) OVER() AS full_count
        FROM campaign
        {where}
        ORDER BY created_at DESC
        LIMIT ${limit_idx} OFFSET ${offset_idx}
        """,
        values,
    )


def update_campaign_status_query(
    campaign_id: str, status: str
) -> Tuple[str, List[Any]]:
    return (
        f"""
        UPDATE campaign SET status = $2, updated_at = now()
        WHERE id = $1::UUID
        RETURNING {CAMPAIGN_COLUMNS}
        """,
        [campaign_id, status],
    )


def update_campaign_totals_query(
    campaign_id: str, total_leads: int, skipped_rows_json: str
) -> Tuple[str, List[Any]]:
    return (
        f"""
        UPDATE campaign
        SET total_leads = $2, skipped_rows = $3::jsonb, updated_at = now()
        WHERE id = $1::UUID
        RETURNING {CAMPAIGN_COLUMNS}
        """,
        [campaign_id, total_leads, skipped_rows_json],
    )


def stamp_lead_campaign_query(lead_id: str, campaign_id: str) -> Tuple[str, List[Any]]:
    # lead_call_tracker.id is VARCHAR (not UUID) — no cast on $1.
    return (
        "UPDATE lead_call_tracker SET campaign_id = $2::UUID WHERE id = $1",
        [lead_id, campaign_id],
    )


def campaign_stats_query(campaign_ids: List[str]) -> Tuple[str, List[Any]]:
    """Per-campaign lead aggregates in one pass. `picked` follows the console
    honesty rule: a finished lead counts as picked unless its outcome is
    NO_ANSWER (BUSY etc. count as answered) — and never when it was ABORTed
    (a stopped campaign's leads finish with ABORT and zero attempts; counting
    them as picked showed "Answered 1 / Dialed 0").

    `dialed` = at least one call initiated. attempt_count alone is the WRONG
    predicate for this: the stored column is 0-based (it counts scheduled
    RETRIES — analytics displays attempt_count + 1), so a lead answered on
    its first attempt finishes with attempt_count = 0 and a completed
    campaign showed "0 / N dialed". call_initiated_time is stamped when a
    call is initiated; the attempt_count > 0 arm keeps retried leads counted
    even if that stamp is missing on an old row. ABORTed-without-dialing
    leads have neither, so they stay out."""
    return (
        """
        SELECT
            campaign_id::text AS campaign_id,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE status = 'BACKLOG') AS backlog,
            COUNT(*) FILTER (WHERE status = 'PROCESSING') AS processing,
            COUNT(*) FILTER (WHERE status = 'FINISHED') AS finished,
            COUNT(*) FILTER (
                WHERE attempt_count > 0 OR call_initiated_time IS NOT NULL
            ) AS dialed,
            COUNT(*) FILTER (
                WHERE status = 'FINISHED'
                  AND outcome IS NOT NULL
                  AND outcome NOT IN ('NO_ANSWER', 'ABORT')
            ) AS picked
        FROM lead_call_tracker
        WHERE campaign_id = ANY($1::UUID[])
        GROUP BY campaign_id
        """,
        [campaign_ids],
    )


def campaign_outcome_counts_query(campaign_ids: List[str]) -> Tuple[str, List[Any]]:
    return (
        """
        SELECT campaign_id::text AS campaign_id, outcome, COUNT(*) AS n
        FROM lead_call_tracker
        WHERE campaign_id = ANY($1::UUID[]) AND outcome IS NOT NULL
        GROUP BY campaign_id, outcome
        """,
        [campaign_ids],
    )


def campaign_pending_lead_ids_query(campaign_id: str) -> Tuple[str, List[Any]]:
    return (
        """
        SELECT id FROM lead_call_tracker
        WHERE campaign_id = $1::UUID AND status IN ('BACKLOG', 'PROCESSING')
        """,
        [campaign_id],
    )
