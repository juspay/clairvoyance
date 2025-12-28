"""
Cleanup utilities for stuck or stale call processing.
"""

from datetime import datetime, timedelta, timezone

from app.ai.voice.agents.breeze_buddy.managers.config import get_lead_config
from app.ai.voice.agents.breeze_buddy.managers.outbound_number import (
    release_number,
)
from app.ai.voice.agents.breeze_buddy.managers.retry import schedule_retry
from app.core.logger import logger
from app.database.accessor import (
    acquire_lock_on_lead_by_id,
    get_leads_by_status_and_time_before,
    get_outbound_number_by_id,
    release_lock_on_lead_by_id,
    update_lead_call_completion_details,
)
from app.schemas import LeadCallStatus


async def cleanup_stuck_leads():
    """
    Cleans up leads that are stuck in the PROCESSING state.
    """
    logger.info("Cleaning up stuck leads...")
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    stale_leads = await get_leads_by_status_and_time_before(
        LeadCallStatus.PROCESSING, stale_time
    )

    logger.info(f"Found {len(stale_leads)} stuck leads to clean up.")

    for lead in stale_leads:
        locked_lead = None
        try:
            # Try to acquire lock on this stuck lead to prevent race conditions
            locked_lead = await acquire_lock_on_lead_by_id(lead.id)
            if not locked_lead:
                logger.info(
                    f"Stuck lead {lead.id} is already locked by another process, skipping cleanup."
                )
                continue

            logger.info(f"Successfully locked stuck lead {lead.id} for cleanup.")

            # Close the stuck record so it won't be picked again
            await update_lead_call_completion_details(
                id=locked_lead.id,
                status=LeadCallStatus.FINISHED,
                outcome="UNKNOWN",
                meta_data={"cleanup": "stuck_processing_timeout"},
                call_end_time=datetime.now(timezone.utc),
            )

            if locked_lead.outbound_number_id:
                outbound_number = await get_outbound_number_by_id(
                    locked_lead.outbound_number_id
                )
                if outbound_number:
                    await release_number(outbound_number.id, outbound_number.provider)

            config = await get_lead_config(locked_lead)
            if config:
                await schedule_retry(locked_lead, config)

        except Exception as e:
            logger.error(f"Error cleaning up stuck lead {lead.id}: {e}")

        finally:
            if locked_lead:
                await release_lock_on_lead_by_id(locked_lead.id)
