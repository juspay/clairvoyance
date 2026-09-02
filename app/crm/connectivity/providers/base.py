"""The ports a provider package implements — one per face.

A connector is not one thing. WhatsApp is a send adapter and an onboarding
handshake, and the two answer to different callers: send.py drives the
first, connectors.py the second. Naming them as separate ports is what lets
`onboarding.py` stay generic — the vocabulary dispatches through a registry
instead of branching on `if connector == "whatsapp"`.

    ChannelAdapter       build the request, read the answer      -> send.py
    ConnectorOnboarder   turn a signup payload into a door       -> connectors.py

A third face — registering the message shapes a provider pre-approves —
joins them with the template registry.

The split that matters for both: a provider CLASSIFIES or NORMALISES, it
never DECIDES. An adapter reports what the provider did and
dispatch.plan_for_outcome turns that into queued / failed / dead; an
onboarder reports how far up the health ladder it got and onboarding.py
turns that into a status. A provider reaching for policy would give each
channel its own private answer to "why", and there would stop being one.
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Dict, Optional, Protocol

import httpx

from app.core.logger import logger
from app.crm.connectivity.reasons import REASON_TRANSPORT
from app.crm.connectivity.schemas import (
    CredentialBundle,
    OnboardResult,
    QueuedMessage,
    SendOutcome,
    SendRoute,
)
from app.crm.shared.redact import mask_address

# All REASON_* words live in reasons.py — one file, one name per failure
# mode.


class ChannelAdapter(ABC):
    """One provider's send face, behind one method.

    Subclasses set ``channel`` and implement ``deliver``. They receive
    everything already resolved — endpoint, decrypted secrets, the approved
    template's language — so no adapter touches the database, which is what
    makes them testable without one.
    """

    #: The channel word this adapter serves, e.g. "whatsapp". The registry
    #: keys on it; it is the vocabulary the tables deliberately do not store.
    channel: ClassVar[str] = ""

    @abstractmethod
    async def deliver(self, message: QueuedMessage, route: SendRoute) -> SendOutcome:
        """Hand ``message`` to the provider and report what happened.

        The whole route arrives as one object rather than as unpacked
        arguments: it is what send() resolved, it grows (a template's
        language today, a quality tier and a pacing budget next), and every
        new field would otherwise churn this signature and every adapter
        with it.

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


class ConnectorHandshakeError(Exception):
    """A provider refused a step of its onboarding handshake.

    The type every ConnectorOnboarder raises, so generic code can tell a
    refusal it may repeat to the merchant from a bug it must not. Messages
    on this type are written FOR the merchant — "that number is not on this
    account" — and onboarding.py passes them through; anything else becomes
    a fixed sentence, because an arbitrary exception's text is an internal
    detail and the API response is not the place to learn it.
    """


class ConnectorOnboarder(Protocol):
    """One provider's onboarding handshake — everything between "the merchant
    clicked connect" and "we hold a usable account".

    ``gather`` does the provider talking and returns FACTS; it writes
    nothing. onboarding.py owns the credential and the atom, so the same
    four database steps serve every connector that will ever exist.
    """

    async def gather(self, request: Any) -> OnboardResult:
        """Walk the provider's handshake and report what it produced.

        ``request`` is the connector's own request model, named by its
        ConnectorSpec — the route validated it before this was called.

        Raises the module's own error type on a provider refusal; onboarding
        turns that into a 400. It must NOT raise for a partial success: a
        handshake that authenticated but could not subscribe reports a lower
        ``health_level`` with a ``health_why``, and the door is written
        degraded rather than not written at all.
        """

    def identify(self, request: Any) -> tuple:
        """(external_account_id, address) from the request alone — PURE.

        No network, so generic code can run the refusals that do not need a
        provider (a disabled door, a retired endpoint) BEFORE gather() spends
        anything irreversible. For WhatsApp that is two fields off the body;
        for a connector whose ids only exist after the handshake, return
        (None, None) and the pre-check simply finds nothing to refuse.

        ``address`` is None for a connector with no channel — a door with no
        pipe has no endpoint to check.
        """

    async def revoke(self, bundle: CredentialBundle, external_account_id: str) -> None:
        """Best-effort: tell the provider to stop sending us this account's
        events. Called BEFORE the disconnect atom, outside it, like every
        other provider call — a failure here logs and the disconnect
        proceeds, because refusing to disconnect locally when the provider
        is unreachable would trap the merchant.
        """


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
