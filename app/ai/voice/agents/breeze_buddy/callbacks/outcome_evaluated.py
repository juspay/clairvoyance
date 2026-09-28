"""The opt-in second merchant webhook: ``call.outcome_evaluated``.

When the post-call eval has written its outcome onto a lead
(lct_accessor.announce_call_evaluated) and the template opted in
(evaluation_config.configuration.notify_webhook, admin-only), the merchant
receives one more webhook for the same call. It repeats the legacy fields the
first webhook sent — so a merchant that processes it again reaches the same
answer — plus ``event`` and the eval's verdict. Only opted-in templates ever
get it, so it is not behind the WEBHOOK_CALL_OUTCOME_KEYS switch.

Plan: docs/CALL_OUTCOMES.md (Phase 2, 2f; section 9).
"""

from datetime import timezone
from typing import Any, Dict

from app.ai.voice.agents.breeze_buddy.utils.call_outcome_webhook import (
    EVENT_CALL_OUTCOME_EVALUATED,
    call_outcome_webhook_keys,
)
from app.ai.voice.agents.breeze_buddy.utils.common import send_webhook_with_retry
from app.core.concurrency import spawn_background_task
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor.breeze_buddy import lead_call_tracker as lct_accessor
from app.schemas import LeadCallTracker
from app.schemas.breeze_buddy.outcomes import call_outcome_from_lead


def outcome_evaluated_payload(lead: LeadCallTracker) -> Dict[str, Any]:
    """The second webhook's body: the first webhook's legacy fields, then the
    event and the call outcome keys (including the eval's verdict)."""
    call_duration = None
    if lead.call_initiated_time and lead.call_end_time:
        call_duration = (
            lead.call_end_time.astimezone(timezone.utc)
            - lead.call_initiated_time.astimezone(timezone.utc)
        ).total_seconds()
    legacy = {
        "callSid": lead.call_id,
        "outcome": lead.outcome,
        "attemptCount": lead.attempt_count + 1,
        "callDuration": call_duration,
        "orderId": lead.request_id,
    }
    keys = call_outcome_webhook_keys(
        call_outcome_from_lead(lead),
        event=EVENT_CALL_OUTCOME_EVALUATED,
        eval_outcome=lead.eval_outcome,
        eval_status=lead.eval_status,
    )
    return {**keys, **legacy}


def _outcome_evaluated_webhook_tap(
    lead: LeadCallTracker, notify_webhook: bool = False
) -> None:
    """Send the second webhook when the template opted in and the lead names
    a reporting URL. Fail-open: never breaks the eval's write."""
    try:
        raw_url = (lead.payload or {}).get("reporting_webhook_url")
        if not notify_webhook or not isinstance(raw_url, str) or not raw_url:
            return
        url: str = raw_url

        async def _send() -> None:
            async with create_aiohttp_session() as session:
                await send_webhook_with_retry(
                    session,
                    url,
                    outcome_evaluated_payload(lead),
                    merchant_id=lead.merchant_id,
                    webhook="call_outcome_evaluated",
                )

        spawn_background_task(_send(), name=f"outcome-evaluated-webhook-{lead.id}")
    except Exception:  # fail-open
        logger.opt(exception=True).error(
            f"call.outcome_evaluated webhook tap failed for {lead.id}"
        )


# Installed on import (callbacks/__init__.py imports this module).
lct_accessor.register_evaluated_hook(_outcome_evaluated_webhook_tap)
