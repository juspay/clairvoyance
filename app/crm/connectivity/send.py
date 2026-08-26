"""The provider seam — a dummy for now.

By convention the module's only provider call site: keeping real channel
adapters behind this file is what lets the manifest answer "did we contact
this person?" without auditing the whole module.

A real adapter maps the provider's response onto SendOutcome:
accepted with the provider's message id, or failed with its error code and
whether retrying could plausibly differ (429 and 5xx yes, bad template no).

TODO before the first real adapter: wrap this call in asyncio.wait_for at the
dispatcher's call site — one timeout no adapter can forget — well under
CRM_DISPATCH_STALE_MINUTES, and treat a timeout as a retryable failure. A send
that outlives its lease gets claimed by a second worker while the first is
still sending, and the customer receives the message twice. A dummy send
cannot exceed the lease, which is the only reason nothing enforces this yet.
"""

from uuid import uuid4

from app.core.logger import logger
from app.crm.connectivity.schemas import QueuedMessage, SendOutcome


async def send(message: QueuedMessage) -> SendOutcome:
    """Pretend to hand ``message`` to its provider.

    Change the return value by hand to exercise the retry and give-up paths.
    """
    provider_message_id = f"dummy-{uuid4().hex[:16]}"
    # Address masked and variable values omitted — no PII in logs.
    logger.info(
        f"[dummy send] channel={message.channel} "
        f"to=***{message.sent_to_address[-4:]} "
        f"purpose={message.purpose_key} template={message.template_id} "
        f"variables={sorted(message.variables)} "
        f"merchant={message.merchant_id} message={message.id} "
        f"-> {provider_message_id}"
    )
    return SendOutcome(status="accepted", provider_message_id=provider_message_id)
