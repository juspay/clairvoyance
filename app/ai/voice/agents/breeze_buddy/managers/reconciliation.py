"""
Channel reconciliation — periodic safety net for leaked outbound number channels.

Resets channel counts to match actual active calls (PROCESSING leads).
Runs via BackgroundTaskScheduler every 5 minutes. Idempotent and safe.
"""

from app.core.logger import logger
from app.database.queries import run_parameterized_query


async def reconcile_outbound_channels() -> None:
    """Reset outbound number channel counts to match actual PROCESSING leads.

    For each outbound number, counts how many leads are actively in PROCESSING
    state and resets the channel count to that value. This fixes any leaked
    channels from missed _release_number calls.
    """
    query = """
        WITH active_counts AS (
            SELECT
                "outbound_number_id",
                COUNT(*) AS actual_active
            FROM "lead_call_tracker"
            WHERE "status" = 'PROCESSING'
            AND "outbound_number_id" IS NOT NULL
            GROUP BY "outbound_number_id"
        )
        UPDATE "outbound_number" o
        SET
            "channels" = COALESCE(ac.actual_active, 0),
            "updated_at" = NOW()
        FROM (
            SELECT o2.id, ac2.actual_active
            FROM "outbound_number" o2
            LEFT JOIN active_counts ac2 ON o2.id = ac2.outbound_number_id
            WHERE o2."channels" IS NOT NULL
            AND o2."channels" != COALESCE(ac2.actual_active, 0)
        ) AS mismatched
        LEFT JOIN active_counts ac ON mismatched.id = ac.outbound_number_id
        WHERE o.id = mismatched.id
        AND o."updated_at" < NOW() - INTERVAL '60 seconds'
        RETURNING o.id, o."channels" AS new_channels, mismatched.id;
    """

    try:
        result = await run_parameterized_query(query, [])
        if result:
            for row in result:
                logger.info(
                    f"Reconciled outbound number {row['id']}: channels → {row['new_channels']}"
                )
            logger.info(f"Channel reconciliation: corrected {len(result)} numbers")
        else:
            logger.debug("Channel reconciliation: all channels in sync")
    except Exception as e:
        logger.error(f"Channel reconciliation failed: {e}", exc_info=True)
