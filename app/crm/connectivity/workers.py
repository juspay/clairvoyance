"""The dispatcher role's claim, wrapping WhatsApp template sync as
housekeeping on the same loop (mirrors app/crm/outreach/workers.py's
retention-sweep pattern) — the pod still runs exactly one loop (design/
worker-runtime.md: "no pod runs a loop AND serves API traffic").

The sync tick is wrapped so a housekeeping failure (a DB blip, Meta being
down) never propagates out of claim_sends() — that would stop the
dispatcher from sending messages because a template sync failed.
"""

import time
from typing import List

from app.core.config.static import CRM_TEMPLATE_SYNC_INTERVAL_SECONDS
from app.core.logger import logger
from app.crm.connectivity import dispatch
from app.crm.connectivity.dispatch import dispatch_send
from app.crm.connectivity.schemas import QueuedMessage
from app.crm.connectivity.templates import sync_all_installations

_last_sync_at = float("-inf")


async def claim_sends(batch: int) -> List[QueuedMessage]:
    global _last_sync_at
    now = time.monotonic()
    if now - _last_sync_at >= CRM_TEMPLATE_SYNC_INTERVAL_SECONDS:
        _last_sync_at = now
        try:
            await sync_all_installations()
        except Exception as e:
            logger.error(f"crm template sync tick failed: {e}")
    # dispatch.claim_sends() resets log context to its own component first —
    # nothing from the sync tick above leaks into the claim/send log lines.
    return await dispatch.claim_sends(batch)


__all__ = ["claim_sends", "dispatch_send"]
