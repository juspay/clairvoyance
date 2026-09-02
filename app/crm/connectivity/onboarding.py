"""Connector onboarding — generic, for every connector there will ever be.

Four steps, none of them provider-shaped:

    merchant lookup  ->  spec.onboarder.gather()  ->  credential  ->  the atom

The provider talking happens inside `gather()`, in that connector's own
package. What comes back is an OnboardResult: an account id, an endpoint, a
bundle, an expiry, and how far up canon T11's health ladder the handshake
got. This file turns that into rows.

Two orderings here are load-bearing and neither is obvious.

**The merchant is checked FIRST**, before the provider is touched at all.
Meta's Embedded Signup code is single-use: spending it on a merchant_id that
turns out not to exist means the merchant has to redo the whole signup, and
Meta is left with our app subscribed to a WABA we then refused.

**The credential is written BEFORE the atom, and the provider calls happen
outside it.** The vault is a different system; holding a database
transaction open across an HTTP call is the thing the worker rules forbid,
and the atom that remains — a door and its first pipe — is exactly the pair
that must share a fate.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.core.logger.context import update_log_context
from app.crm.connectivity import accounts
from app.crm.connectivity.connectors import (
    ConnectorHandshakeError,
    ConnectorSpec,
    connector_for,
)
from app.crm.connectivity.db import DbTxn, UniqueViolation, atomically
from app.crm.connectivity.db.accessors import (
    binding as binding_accessor,
    installation as installation_accessor,
)
from app.crm.connectivity.schemas.connector import (
    ConnectorInstallation,
    InstallationRead,
    OnboardResult,
)
from app.crm.connectivity.status import (
    BINDING_RETIRED,
    INSTALLATION_CONNECTING,
    INSTALLATION_DEGRADED,
    INSTALLATION_DISABLED,
    INSTALLATION_HEALTHY,
)
from app.database.accessor.breeze_buddy.credentials import (
    create_credential,
    get_credential_by_name,
    update_credential,
)
from app.database.accessor.breeze_buddy.merchants import get_merchants_by_ids
from app.schemas import CredentialType

#: canon T11's ladder -> the traffic light on the row. Only 'subscribed' and
#: above may show green: a door with no event subscription receives no
#: delivery receipts and no inbound STOP, and send() fails closed on anything
#: but 'healthy', which is the correct posture for a connection that cannot
#: hear the person it is contacting.
_STATUS_FOR_HEALTH = {
    "configured": INSTALLATION_CONNECTING,
    "authenticated": INSTALLATION_DEGRADED,
    "subscribed": INSTALLATION_HEALTHY,
    "healthy": INSTALLATION_HEALTHY,
}


class OnboardingError(Exception):
    """Onboarding refused before, or instead of, writing a connection."""


class UnknownConnectorError(OnboardingError):
    """No such connector_key — the registry IS the vocabulary, so this is a
    404 rather than a bad request."""


def credential_name(connector_key: str, merchant_id: str, account_id: str) -> str:
    """The vault has no merchant column — only reseller_id — so the NAME has
    to carry the tenancy, or two merchants under one reseller collide on one
    credential row and the second onboarding overwrites the first's token."""
    return f"{connector_key}:{merchant_id}:{account_id}"


def _health_detail(result: OnboardResult) -> Dict[str, Any]:
    """canon T11: status is the traffic light, health_detail the sentence
    under it, and a why is MANDATORY below healthy."""
    return {
        "level": result.health_level,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "why": result.health_why,
    }


async def _refuse_before_spending(
    merchant_id: str, spec: ConnectorSpec, connector_key: str, request: Any
) -> None:
    """The two refusals that need no provider call, run first.

    Both are re-checked inside the atom, where they are race-safe. This is
    the cheap answer, not the authoritative one — a connector whose ids are
    only knowable after the handshake returns (None, None) and skips it.
    """
    account_id, address = spec.onboarder.identify(request)
    if account_id:
        existing = await installation_accessor.get_installation_by_account(
            merchant_id, connector_key, account_id
        )
        if existing is not None and existing.status == INSTALLATION_DISABLED:
            raise OnboardingError(
                "this connection is disabled — an administrator must re-enable "
                "it before it can be reconnected"
            )
    if spec.channel and address:
        binding = await binding_accessor.peek_binding_by_address(
            merchant_id, spec.channel, address
        )
        if binding is not None and binding.status == BINDING_RETIRED:
            raise OnboardingError("this endpoint was retired and cannot be reconnected")


async def _merchant_reseller(merchant_id: str) -> Optional[str]:
    """The reseller the vault will scope this credential to. Raises if the
    merchant does not exist — the cheap fact, gathered before anything
    irreversible."""
    merchants, _ = await get_merchants_by_ids([merchant_id])
    if not merchants:
        raise OnboardingError("unknown merchant")
    return merchants[0].reseller_id


async def _store_credential(
    reseller_id: Optional[str],
    name: str,
    bundle: Dict[str, Any],
    description: str,
) -> str:
    """Write or rotate the bundle, and return the vault row's id.

    Idempotent by name: a second onboarding of the same account rotates the
    existing row rather than adding another, so the installation's pointer
    stays valid throughout.
    """
    existing = await get_credential_by_name(reseller_id, name, mask=False)
    if existing is not None:
        credential = await update_credential(
            existing.id, credential_type=CredentialType.CUSTOM, value=bundle
        )
    else:
        credential = await create_credential(
            reseller_id,
            name,
            CredentialType.CUSTOM,
            bundle,
            description=description,
        )
    if credential is None:
        raise OnboardingError("could not store the connector credential")
    return str(credential.id)


async def onboard(
    merchant_id: str, connector_key: str, payload: Dict[str, Any]
) -> InstallationRead:
    """Connect one merchant to one connector account.

    Idempotent: re-running it for the same account rotates the credential and
    updates the door in place. The schema's own unique indexes enforce that,
    not this function's care.
    """
    spec = connector_for(connector_key)
    if spec is None:
        raise UnknownConnectorError(f"unknown connector '{connector_key}'")

    update_log_context(connector_key=connector_key)
    request = spec.request_model(**payload)

    # Everything cheap and refusable happens before the one-shot code is
    # spent: the merchant must exist, the door must not be switched off, and
    # the endpoint must not have been retired. The atom re-checks the last
    # two — those are the race-safe reads — but discovering them there means
    # the merchant has already burned a signup code and Meta is already
    # subscribed to an account we then refuse.
    reseller_id = await _merchant_reseller(merchant_id)
    await _refuse_before_spending(merchant_id, spec, connector_key, request)

    try:
        result = await spec.onboarder.gather(request)
    except OnboardingError:
        raise
    except ConnectorHandshakeError as e:
        # The provider's DECLARED refusal. Its message is written for the
        # merchant ("that number is not on this account"), so it is passed
        # through — that sentence is the whole value of the 400.
        logger.warning(f"onboarding: {connector_key} handshake refused — {e}")
        raise OnboardingError(str(e)) from e
    except Exception as e:
        # Anything else is a bug, and a bug's text is an internal detail: a
        # KeyError or a driver message in an API response tells the caller
        # nothing they can act on and tells everyone else about our
        # internals. Logged in full, answered with one fixed sentence.
        logger.opt(exception=e).error(
            f"onboarding: {connector_key} handshake raised unexpectedly"
        )
        raise OnboardingError("could not complete the connector handshake") from e
    logger.info(
        f"onboarding: {connector_key} handshake reached '{result.health_level}' "
        f"for merchant {merchant_id}"
    )

    credential_id = await _store_credential(
        reseller_id,
        credential_name(connector_key, merchant_id, result.external_account_id),
        result.bundle,
        f"{connector_key} credentials — merchant {merchant_id}, "
        f"account {result.external_account_id}",
    )

    try:
        installation = await atomically(
            _onboard_in_txn, merchant_id, spec, connector_key, result, credential_id
        )
    except UniqueViolation as e:
        # Two onboards for the same merchant and channel can both read "no
        # active primary" and both try to write one. The partial unique index
        # (merchant_id, channel) WHERE is_primary is what actually keeps a
        # merchant to one default route, and it holds: the loser's whole
        # transaction rolls back, so nothing is half-written.
        #
        # What was wrong was the ANSWER — an unhandled violation is a 500 for
        # a caller who did nothing wrong. Retrying succeeds, because by then
        # a primary exists and this number is written as a non-default route.
        # An advisory lock would also close it, at the cost of serialising
        # every onboarding to prevent a collision between two that happen in
        # the same instant.
        logger.warning(
            f"onboarding: {merchant_id}/{spec.channel} raced another "
            f"connection for the default route — {e}"
        )
        raise OnboardingError(
            "another connection for this channel was being set up at the "
            "same time — try again"
        ) from e
    logger.info(
        f"onboarding: installation {installation.id} is '{installation.status}'"
    )
    return installation


async def _onboard_in_txn(
    txn: DbTxn,
    merchant_id: str,
    spec: ConnectorSpec,
    connector_key: str,
    result: OnboardResult,
    credential_id: str,
) -> InstallationRead:
    """ATOMIC: the door and its first pipe — a half-onboarded merchant (an
    installation nothing can send from, or a binding hanging off no
    installation) must never be visible, because both halves report success
    on the connections screen while every send refuses.

    A connector with no channel writes the door alone, and that is a COMPLETE
    onboarding: a data connector (Shopify, Zendesk) has nothing to send from,
    so there is no pipe to be half of."""
    status = _STATUS_FOR_HEALTH.get(result.health_level, INSTALLATION_DEGRADED)
    installation = await installation_accessor.upsert_installation(
        txn,
        merchant_id,
        connector_key,
        result.external_account_id,
        result.display_label,
        credential_id,
        status,
        result.token_expires_at,
        json.dumps(_health_detail(result)),
    )
    if installation is None:
        # The upsert's WHERE declined: the existing row is 'disabled', an ops
        # decision that pressing "connect" again must not undo.
        raise OnboardingError(
            "this connection is disabled — an administrator must re-enable it "
            "before it can be reconnected"
        )

    if spec.channel is None:
        # A door with no pipe. Nothing below applies: there is no address to
        # bind, no default route to pick, and no send path to protect.
        return installation

    address = result.address
    if not address:
        raise OnboardingError(
            f"the {spec.channel} connector returned no endpoint to bind"
        )

    existing = await binding_accessor.get_binding_by_address(
        txn, merchant_id, spec.channel, address
    )
    if existing is not None and existing.status == BINDING_RETIRED:
        # canon T12: a retired pipe SURRENDERED its address, and the provider
        # may have recycled it to somebody else. Resurrecting it would point
        # this merchant's sends at a stranger's endpoint.
        raise OnboardingError("this endpoint was retired and cannot be reconnected")

    # Only the FIRST pipe on a channel becomes the default. Connecting a
    # second number is not a decision to make it the default route.
    already_default = await binding_accessor.has_active_primary_binding(
        txn, merchant_id, spec.channel
    )
    await binding_accessor.upsert_binding(
        txn,
        merchant_id,
        spec.channel,
        installation.id,
        address,
        not already_default,
    )
    return installation


async def get_installation(
    merchant_id: str, installation_id: str
) -> Optional[InstallationRead]:
    return await installation_accessor.get_installation_read(
        merchant_id, installation_id
    )


async def list_installations(merchant_id: str) -> List[InstallationRead]:
    return await installation_accessor.list_installations(merchant_id)


async def disconnect(
    merchant_id: str, installation_id: str
) -> Optional[InstallationRead]:
    """Revoke a connection. Never deletes — history is the point of this
    table, and manifest rows point at the pipes under it.

    Returns None when the installation is not this merchant's (fail closed on
    tenancy: an unknown id and another tenant's id are one answer).

    The provider is told BEFORE the atom and outside it. Leaving a merchant's
    account subscribed means the provider keeps delivering events for someone
    who left, and every one of them is attributed to a revoked door.
    """
    installation = await installation_accessor.get_installation(
        merchant_id, installation_id
    )
    if installation is None:
        return None

    await _revoke_at_provider(installation.connector_key, installation)

    return await atomically(_disconnect_in_txn, merchant_id, installation_id)


async def _revoke_at_provider(
    connector_key: str, installation: ConnectorInstallation
) -> None:
    """Best effort, and deliberately so: a provider being unreachable must not
    trap a merchant in a connection they asked to end.

    The vault row is left ACTIVE with a live token on purpose. Deactivating it
    would strand a re-onboard of the same account — the credential is keyed by
    name and rotated in place, so the next connect reuses this row. The token
    lives until the provider expires it, and a revoked installation refuses
    sends regardless.
    """
    spec = connector_for(connector_key)
    if spec is None or not installation.credential_id:
        return
    try:
        bundle = await accounts.bundle_for(installation)
        await spec.onboarder.revoke(bundle, installation.external_account_id)
    except accounts.AccountError:
        # No usable credential to revoke WITH. Nothing to tell the provider,
        # and nothing to warn about — the local disconnect proceeds.
        return
    except Exception as e:
        logger.opt(exception=e).warning(
            f"disconnect: could not revoke {connector_key} subscription for "
            f"installation {installation.id}"
        )


async def _disconnect_in_txn(
    txn: DbTxn, merchant_id: str, installation_id: str
) -> Optional[InstallationRead]:
    """ATOMIC: revoking the door and pausing its pipes share one fate — a
    revoked installation must never leave a binding that still claims to be
    an active send route, and a paused binding that kept is_primary blocks
    the merchant from ever connecting another number on that channel."""
    installation = await installation_accessor.revoke_installation(
        txn, merchant_id, installation_id
    )
    if installation is None:
        return None
    paused = await binding_accessor.pause_bindings_for_installation(
        txn, merchant_id, installation_id
    )
    logger.info(
        f"disconnect: installation {installation_id} revoked, "
        f"{len(paused)} binding(s) paused"
    )
    return installation


async def resubscribe(
    merchant_id: str, installation_id: str
) -> Optional[InstallationRead]:
    """Turn a connected account's webhooks (back) on — the recovery door.

    Onboarding subscribes on its happy path but cannot run again: its
    Embedded Signup code is one-shot. So a handshake that authenticated but
    could not subscribe, or an account the provider quietly unsubscribed,
    gets just the subscription step re-run from stored credentials —
    ``disconnect`` with the opposite verb, same gather, then an atom that
    re-stamps health.

    Returns None when the installation is not this merchant's (fail closed
    on tenancy, exactly like disconnect). Raises OnboardingError with a
    sentence a merchant can act on for every other refusal — a silently
    failed subscription looks healthy until somebody wonders why no events
    ever arrive.
    """
    installation = await installation_accessor.get_installation(
        merchant_id, installation_id
    )
    if installation is None:
        return None

    spec = connector_for(installation.connector_key)
    if spec is None:
        # Refusing beats guessing: running Meta's call against a connector
        # that has no subscription step would send a request nothing there
        # understands.
        raise OnboardingError(
            f"This account is a '{installation.connector_key}' connector, "
            f"which has no webhook subscription to turn on."
        )
    if not installation.external_account_id:
        raise OnboardingError("This account has no provider account id to subscribe.")

    try:
        bundle = await accounts.bundle_for(installation)
    except accounts.AccountError:
        # Vault row gone, deactivated, or undecryptable — one fact from
        # here: no usable secret to subscribe with. (A vault OUTAGE raises
        # past this except and surfaces as the route's 500, never as
        # "reconnect your account" advice for a healthy credential.)
        raise OnboardingError(
            "This account's credentials are missing or unreadable. "
            "Reconnect it first."
        )

    try:
        await spec.onboarder.resubscribe(bundle, installation.external_account_id)
    except ConnectorHandshakeError as e:
        # The provider's own refusal, passed through: the merchant's "why"
        # gets the provider's words, not our paraphrase.
        logger.error(f"resubscribe: {installation.connector_key} refused — {e}")
        raise OnboardingError(str(e)) from e

    return await atomically(_resubscribe_in_txn, merchant_id, installation_id)


async def _resubscribe_in_txn(
    txn: DbTxn, merchant_id: str, installation_id: str
) -> Optional[InstallationRead]:
    """ATOMIC: the provider confirmed the subscription and the door's light
    must say so in the same breath — a successful recovery that left the row
    'degraded' would keep every send refusing (send fails closed on anything
    but 'healthy'), and a health_detail whose why is now false contradicts
    canon T11 (the light never contradicts the sentence)."""
    detail = {
        "level": "subscribed",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "why": None,
    }
    return await installation_accessor.update_installation_health(
        txn,
        merchant_id,
        installation_id,
        status=_STATUS_FOR_HEALTH["subscribed"],
        health_detail=json.dumps(detail),
    )
