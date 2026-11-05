"""Campaign accessors — thin wrappers over the parameterized queries."""

from typing import Dict, List, Optional

from app.core.logger import logger
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.campaigns import (
    campaign_outcome_counts_query,
    campaign_pending_lead_ids_query,
    campaign_stats_query,
    get_campaign_by_id_query,
    insert_campaign_query,
    list_campaigns_query,
    stamp_lead_campaign_query,
    update_campaign_status_query,
    update_campaign_totals_query,
)
from app.schemas.breeze_buddy.campaigns import Campaign, CampaignStats


def _decode(row) -> Campaign:
    return Campaign(
        id=str(row["id"]),
        reseller_id=row["reseller_id"],
        merchant_id=row["merchant_id"],
        template_id=str(row["template_id"]),
        name=row["name"],
        status=row["status"],
        total_leads=row["total_leads"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create_campaign(
    reseller_id: str,
    merchant_id: Optional[str],
    template_id: str,
    name: str,
    created_by: Optional[str],
) -> Optional[Campaign]:
    try:
        q, v = insert_campaign_query(
            reseller_id, merchant_id, template_id, name, created_by
        )
        rows = await run_parameterized_query(q, v)
        return _decode(rows[0]) if rows else None
    except Exception as e:
        logger.error(f"Error creating campaign '{name}': {e}", exc_info=True)
        raise


async def get_campaign_by_id(campaign_id: str) -> Optional[Campaign]:
    try:
        q, v = get_campaign_by_id_query(campaign_id)
        rows = await run_parameterized_query(q, v)
        return _decode(rows[0]) if rows else None
    except Exception as e:
        logger.error(f"Error fetching campaign {campaign_id}: {e}", exc_info=True)
        raise


async def list_campaigns(
    reseller_ids: Optional[List[str]],
    merchant_ids: Optional[List[str]],
    page: int,
    limit: int,
) -> tuple[List[Campaign], int]:
    try:
        q, v = list_campaigns_query(reseller_ids, merchant_ids, page, limit)
        rows = await run_parameterized_query(q, v)
        total = int(rows[0]["full_count"]) if rows else 0
        return [_decode(r) for r in rows], total
    except Exception as e:
        logger.error(f"Error listing campaigns: {e}", exc_info=True)
        raise


async def update_campaign_status(campaign_id: str, status: str) -> Optional[Campaign]:
    try:
        q, v = update_campaign_status_query(campaign_id, status)
        rows = await run_parameterized_query(q, v)
        return _decode(rows[0]) if rows else None
    except Exception as e:
        logger.error(
            f"Error updating campaign {campaign_id} -> {status}: {e}", exc_info=True
        )
        raise


async def update_campaign_totals(
    campaign_id: str, total_leads: int, skipped_rows_json: str
) -> Optional[Campaign]:
    try:
        q, v = update_campaign_totals_query(campaign_id, total_leads, skipped_rows_json)
        rows = await run_parameterized_query(q, v)
        return _decode(rows[0]) if rows else None
    except Exception as e:
        logger.error(
            f"Error updating campaign totals {campaign_id}: {e}", exc_info=True
        )
        raise


async def stamp_lead_campaign(lead_id: str, campaign_id: str) -> None:
    q, v = stamp_lead_campaign_query(lead_id, campaign_id)
    await run_parameterized_query(q, v)


async def get_campaign_stats(campaign_ids: List[str]) -> Dict[str, CampaignStats]:
    """One aggregate pass for a page of campaigns; ids absent from the result
    simply have no leads yet."""
    if not campaign_ids:
        return {}
    try:
        stats: Dict[str, CampaignStats] = {}
        q, v = campaign_stats_query(campaign_ids)
        for row in await run_parameterized_query(q, v):
            stats[row["campaign_id"]] = CampaignStats(
                total=row["total"],
                backlog=row["backlog"],
                processing=row["processing"],
                finished=row["finished"],
                dialed=row["dialed"],
                picked=row["picked"],
            )
        q, v = campaign_outcome_counts_query(campaign_ids)
        for row in await run_parameterized_query(q, v):
            entry = stats.setdefault(row["campaign_id"], CampaignStats())
            entry.outcome_counts[row["outcome"]] = int(row["n"])
        return stats
    except Exception as e:
        logger.error(f"Error aggregating campaign stats: {e}", exc_info=True)
        raise


async def get_campaign_pending_lead_ids(campaign_id: str) -> List[str]:
    q, v = campaign_pending_lead_ids_query(campaign_id)
    rows = await run_parameterized_query(q, v)
    return [str(r["id"]) for r in rows]
