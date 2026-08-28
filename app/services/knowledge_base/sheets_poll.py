"""
Scheduled Google Sheets freshness poller.

Runs on the BackgroundTaskScheduler (distributed lock -> one pod per tick).
For every READY google_sheet document past the debounce floor it does ONE
cheap Drive ``files.get(modifiedTime)`` call; changed sheets are flipped back
to PENDING and the ingestion worker re-reads them (chunk-hash diff re-embeds
only edited rows). Merchants can always force it sooner with "Sync now".
"""

import asyncio

from app.core.config.dynamic import (
    KB_SHEETS_MIN_SYNC_INTERVAL_SECONDS,
    KB_SHEETS_POLL_BATCH_SIZE,
    KB_SHEETS_POLL_ENABLED,
    KB_SHEETS_PROBE_SPACING_SECONDS,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.knowledge_base import (
    get_sheet_documents_due_for_poll,
    mark_kb_document_for_resync,
)
from app.services.knowledge_base.connectors import get_connector
from app.services.knowledge_base.ingestion import kick_ingestion


async def poll_sheet_documents() -> int:
    """Probe connected sheets for changes; returns the number requeued."""
    if not await KB_SHEETS_POLL_ENABLED():
        return 0

    min_age = await KB_SHEETS_MIN_SYNC_INTERVAL_SECONDS()
    batch_size = await KB_SHEETS_POLL_BATCH_SIZE()
    probe_spacing = await KB_SHEETS_PROBE_SPACING_SECONDS()
    documents = await get_sheet_documents_due_for_poll(min_age, batch_size)
    if not documents:
        return 0

    connector = get_connector("google_sheet")
    changed = 0
    for document in documents:
        try:
            if await connector.detect_change(document):
                requeued = await mark_kb_document_for_resync(document.id)
                if requeued is not None:
                    changed += 1
                    logger.info(
                        f"Sheet document {document.id} ('{document.name}') "
                        "changed at source; queued for re-sync"
                    )
        except Exception as e:
            logger.warning(f"Sheets poll probe failed for document {document.id}: {e}")
        await asyncio.sleep(probe_spacing)

    if changed:
        kick_ingestion()
    return changed
