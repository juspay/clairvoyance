"""The parent every channel adapter inherits.

A child implements two things — build the request, read the answer — and
inherits what must never vary between channels: transport-failure
classification, address masking, and the fact that policy is not its job.

The split that matters: an adapter CLASSIFIES, it never DECIDES. It reports
what the provider did (accepted; failed, plausibly-retryable or not);
dispatch.plan_for_outcome alone turns that into queued / failed / dead.
An adapter reaching for the retry ladder would give each channel its own
private policy, and "why was this retried" would stop having one answer.
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Dict, Optional

import httpx

from app.core.logger import logger
from app.crm.connectivity.reasons import REASON_TRANSPORT
from app.crm.connectivity.schemas import (
    ChannelBinding,
    CredentialBundle,
    QueuedMessage,
    SendOutcome,
)
from app.crm.shared.redact import mask_address

# All REASON_* words live in reasons.py — one file, one name per failure
# mode.


class ChannelAdapter(ABC):
    """One provider, behind one method.

    Subclasses set ``channel`` and implement ``deliver``. They receive
    everything already resolved — endpoint, decrypted secrets — so no adapter
    touches the database, which is what makes them testable without one.
    """

    #: The channel word this adapter serves, e.g. "whatsapp". The registry
    #: keys on it; it is the vocabulary the tables deliberately do not store.
    channel: ClassVar[str] = ""

    @abstractmethod
    async def deliver(
        self,
        message: QueuedMessage,
        route_bundle: CredentialBundle,
        binding: ChannelBinding,
    ) -> SendOutcome:
        """Hand ``message`` to the provider and report what happened.

        Must not raise for anything the provider does — a rejection is a
        SendOutcome, not an exception. Raising is reserved for genuine bugs,
        which send() catches as retryable since it cannot know whether the
        message got out.
        """

    # ---- shared plumbing children inherit --------------------------------

    def transport_failure(self, error: Exception, address: str) -> SendOutcome:
        """Classify a request that never produced a response.

        Always retryable: this covers "no answer", and no answer is not the
        same as no — the customer may already have the message.
        """
        logger.warning(
            f"{self.channel} transport failure to "
            f"{mask_address(address, self.channel)}: "
            f"{type(error).__name__}"
        )
        return SendOutcome(status="failed", reason=REASON_TRANSPORT, retryable=True)

    @staticmethod
    def json_body(response: httpx.Response) -> Dict[str, Any]:
        """The response as an object, or an empty one.

        A provider having a bad day returns HTML from a load balancer, and a
        JSONDecodeError here would read as a code bug rather than the upstream
        failure it is. The status code still carries the verdict, so an empty
        body degrades to "failed, no detail" instead of taking the worker down.
        """
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}


class AdapterRegistryError(LookupError):
    """No adapter serves this channel — raised only by the registry lookup."""


def require_secret(bundle: CredentialBundle, key: str, channel: str) -> Optional[str]:
    """A secret the adapter cannot work without, or None with a log line.

    Split out so every adapter reports a missing key the same way: terminal,
    never retryable — retrying a bundle that lacks the key it needs just
    spends attempts.
    """
    value = bundle.secret(key)
    if value is None:
        logger.error(f"{channel}: credential bundle is missing '{key}'")
    return value
