"""The provider seam — the module's only path to the outside world.

Everything that must be true before a message reaches a person is checked
here, once, for every channel: the gate granted it, the channel has an
adapter, the merchant has a live pipe, the account behind it is healthy, its
credentials decrypt. Any of those missing REFUSES the send — nothing falls
through to a default, because every plausible default (another number,
another account) contacts somebody in a way they did not agree to.

The route is assembled here and handed to the adapter whole, which is what
lets an adapter be tested without a database. Adapters live behind
providers/ and are imported nowhere else — boundary rule 11 fails CI on any
other import, because a stray one reaches a provider without passing the
checks above. The same fact, greppable:

    grep -rn "connectivity.providers" app/ | grep -v "^app/crm/connectivity/providers/"

Two things this file deliberately does NOT do:

  · Decide what a failure means. It reports; dispatch.py's retry ladder
    decides. Two deciders would mean two answers to "why was this retried".
  · Write to the route tables — only READ them. Acting on a credential
    rejection (marking degraded, alerting, re-auth) belongs to channel
    lifecycle code; two owners for one lifecycle is how a status flaps.
"""

import asyncio
from typing import Optional, Union

from app.core.config.static import CRM_MESSAGE_SEND_TIMEOUT_SECONDS
from app.core.logger import logger
from app.crm.connectivity.db.accessors import (
    binding as binding_accessor,
    installation as installation_accessor,
)
from app.crm.connectivity.providers import adapter_for
from app.crm.connectivity.providers.base import ChannelAdapter
from app.crm.connectivity.reasons import (
    REASON_GATE_REFUSED,
    REASON_INSTALLATION_UNHEALTHY,
    REASON_NO_ADAPTER,
    REASON_NO_BINDING,
    REASON_NO_CREDENTIAL,
    REASON_NO_INSTALLATION,
    REASON_SEND_ERROR,
    REASON_TIMEOUT,
)
from app.crm.connectivity.schemas import (
    CredentialBundle,
    QueuedMessage,
    SendOutcome,
    SendRoute,
    SendToken,
)
from app.crm.shared.redact import mask_address
from app.database.accessor.breeze_buddy.credentials import get_credential_by_id

# All REASON_* words live in reasons.py — one file, one name per failure
# mode. This door only raises them.

# The only installation state a send may leave through — fail closed on
# everything else, 'connecting' included. Onboarding (#1038) verifies the
# token and number against the Graph API and writes the row as 'healthy'
# directly, so 'connecting' is an unproven connection with no first-send
# deadlock to earn it an exception.
SENDABLE_INSTALLATION_STATES = frozenset({"healthy"})


def _refused(reason: str) -> SendOutcome:
    """OUR refusal — 'blocked', never 'failed'.

    The manifest distinguishes "we would not" (blocked: no route, no
    adapter, gate said no) from "they would not" (failed: the provider's
    code). A merchant with a paused number must not read the word reserved
    for "Meta said no". Terminal either way.
    """
    return SendOutcome(status="blocked", reason=reason)


def token_grants(token: SendToken, message: QueuedMessage) -> bool:
    """Whether this token authorises THIS message.

    The identity check is the point: a token granting some other message is
    not a weaker grant, it is a bug — one grant reused across a batch would
    authorise customers it never named. Checking it while the gate is still a
    stub means the check is in place and tested before real grants arrive.
    """
    return (
        token.granted
        and token.message_id == message.id
        and token.purpose_key == message.purpose_key
    )


async def resolve_send_route(
    merchant_id: str, channel: str, binding_id: Optional[str] = None
) -> Union[SendRoute, str]:
    """Find the pipe, the account and the secrets — or the reason there is none.

    Returns a SendRoute, or a refusal reason as a string. Not an exception:
    "this merchant has not connected WhatsApp" is an ordinary answer that
    belongs on the manifest row.

    Order matters — the binding comes first because it is the merchant-scoped
    anchor, and everything after is reached THROUGH it, so no lookup here can
    wander into another tenant's rows.

    TODO(T23): once the template registry lands, look the template up here
    and refuse with blocked/template_not_approved unless it is approved on
    this account — taking the language from the registry row instead of
    binding.capabilities (ADR 0011: a non-approved template is refused
    BEFORE the provider call).
    """
    binding = await binding_accessor.get_binding(merchant_id, channel, binding_id)
    if binding is None:
        # Never connected, paused, retired, or another merchant's row: from
        # the sender's side those are one fact, so they get one reason.
        return REASON_NO_BINDING

    installation = await installation_accessor.get_installation(
        merchant_id, binding.installation_id
    )
    if installation is None:
        return REASON_NO_INSTALLATION
    if installation.status not in SENDABLE_INSTALLATION_STATES:
        logger.warning(
            f"connectivity: installation {installation.id} is "
            f"'{installation.status}' — no route for {merchant_id}/{channel}"
        )
        return REASON_INSTALLATION_UNHEALTHY

    if not installation.credential_id:
        return REASON_NO_CREDENTIAL
    # The vault (`credentials`) belongs to app/database, so it is read through
    # ITS accessor, never raw SQL from here (table-ownership law). mask=False:
    # the adapter needs the real secret, not the API's ****. raise_errors=True:
    # None must mean "row gone/dead" (terminal below) — a pool blip on this
    # read has to raise and ride send()'s catch into a retryable send_error,
    # like the same blip on any other read in this resolver.
    credential = await get_credential_by_id(
        installation.credential_id, mask=False, raise_errors=True
    )
    if credential is None or not credential.is_active or not credential.value:
        # Vault row gone, deactivated, or it would not decrypt (an
        # undecryptable value decodes as {}) — same thing to this message,
        # and none of it is worth a retry.
        return REASON_NO_CREDENTIAL
    # Ownership cannot be compared HERE: the vault's scope column is
    # reseller_id — one level above merchant (022) — and this module by law
    # knows no reseller. The guard is the merchant-scoped installation fetch
    # above; the onboarding sync that WRITES credential_id owns refusing a
    # foreign reseller's bundle (NULL = global stays legal).
    bundle = CredentialBundle(values=credential.value)

    return SendRoute(installation=installation, binding=binding, bundle=bundle)


async def _resolve_and_deliver(
    adapter: ChannelAdapter, message: QueuedMessage
) -> SendOutcome:
    """The part of a send that must finish inside the claim lease.

    Split out so ONE wait_for in send() covers the route's DB reads as well
    as the provider call: a stalled pool outlives the lease exactly like a
    hung provider — same reassigned row, same double send.
    """
    route = await resolve_send_route(
        message.merchant_id, message.channel, message.binding_id
    )
    if not isinstance(route, SendRoute):
        # No route, and the resolver said why. Its reason is the honest one.
        logger.warning(f"connectivity: message {message.id} refused — {route}")
        return _refused(route)

    logger.info(
        f"connectivity: sending {message.id} via {message.channel} "
        f"to {mask_address(message.sent_to_address, message.channel)} "
        f"from binding {route.binding.id}"
    )
    return await adapter.deliver(message, route)


async def send(send_token: SendToken, message: QueuedMessage) -> SendOutcome:
    """Hand ``message`` to its channel's adapter, if everything permits it."""
    if not token_grants(send_token, message):
        logger.error(
            f"connectivity: send token does not authorise message {message.id}"
        )
        return _refused(REASON_GATE_REFUSED)

    adapter = adapter_for(message.channel)
    if adapter is None:
        # The registry IS the channel vocabulary, so a row naming a channel
        # nothing serves cannot become sendable by waiting.
        logger.error(
            f"connectivity: no adapter for channel '{message.channel}' "
            f"(message {message.id})"
        )
        return _refused(REASON_NO_ADAPTER)

    try:
        return await asyncio.wait_for(
            _resolve_and_deliver(adapter, message),
            timeout=CRM_MESSAGE_SEND_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        # Retryable: "no answer" is not "no", the provider may have taken it.
        # The timeout exists so a hung send cannot outlive the claim lease and
        # hand the row to a second worker mid-send — the one double-send the
        # dedupe key cannot prevent.
        logger.warning(
            f"connectivity: message {message.id} timed out after "
            f"{CRM_MESSAGE_SEND_TIMEOUT_SECONDS}s"
        )
        return SendOutcome(status="failed", reason=REASON_TIMEOUT, retryable=True)
    except Exception as e:
        # The default case. Adapters classify their own failures, so anything
        # landing here escaped classification — it becomes an outcome rather
        # than a raise the caller must guess about, retryable for the same
        # reason as the timeout: we cannot know whether the provider saw it.
        logger.opt(exception=e).error(
            f"connectivity: send raised for message {message.id}"
        )
        return SendOutcome(status="failed", reason=REASON_SEND_ERROR, retryable=True)
