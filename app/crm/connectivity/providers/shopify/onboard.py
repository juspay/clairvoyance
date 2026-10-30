"""Registering a Shopify shop — the relay-era handshake, which is no
handshake at all.

Today nautilus already holds every shop's offline token, so onboarding here
is not an OAuth install: it RECORDS that this merchant has a Shopify shop and
what it is called, so an action has an installation to hang its tenancy on.
No provider call, no credential — ``bundle`` stays empty and the
installation's ``credential_id`` stays NULL, which is precisely what
``actions._transport()`` reads to decide that this shop's writes still travel
by relay.

That NULL is the migration switch. When clairvoyance takes the tokens, this
file grows the real handshake (or a one-time backfill writes the bundle), the
installation gains a credential, and the same shop silently starts going
direct. No plan is republished, because no plan ever named the transport.

``channel=None`` on the spec: a Shopify install is a complete onboarding with
nothing to bind — no address, no pipe, no send path. ``_onboard_in_txn``
already returns early for that shape, so the binding machinery is never
entered.

The shop is the merchant itself. A nautilus merchant's id IS its myshopify
domain, and the relay writes with whichever shop's token the domain names —
so no separate shop field exists: a typed domain would let one merchant's
workflows tag and note another store's orders. ``request.merchant_id`` is the
only source, and onboarding.py refuses a body whose merchant_id is not the one
the token was cleared for.

That holds only for merchants the Breeze Buddy app install created (reseller
BB_SHOPIFY): the install is the proof of the shop. Self-signup and POST
/merchant also accept a myshopify-shaped id, so onboarding.py refuses a
Shopify connect for any other merchant.

Corpus trail: canon/04-connectivity.md T11 makes external_account_id the
PROVIDER's id for the account, so one merchant could hold two shops. Here it
is our merchant id, which means one shop per merchant and our tenant id as
the source of truth. That is right while nautilus keys the tenant by domain;
it changes the day a real Shopify OAuth install lands, when the handshake
names the shop and this file grows it.

Reached only through connectivity/connectors.py (boundary rule 11).
"""

from typing import Any, Optional, Tuple

from pydantic import BaseModel, Field

from app.crm.connectivity.schemas.connector import OnboardResult
from app.crm.connectivity.schemas.message import CredentialBundle


class OnboardShopifyRequest(BaseModel):
    """Body for POST /connectors/shopify/onboard.

    Lives with the face, not in the module's generic schemas: the route takes
    a plain dict and asks the registry which model validates it, so this
    model's only consumer is this package's ConnectorSpec entry.
    """

    merchant_id: str = Field(
        ...,
        description="Tenant scope — required, and must match the merchant this "
        "request is scoped to. A nautilus merchant's id IS its myshopify domain, "
        "so this is also the shop being connected",
    )
    display_label: Optional[str] = Field(
        None, description="What the merchant calls this shop in the console"
    )


class ShopifyOnboarder:
    """The no-op handshake that still produces a real door."""

    def identify(self, request: Any) -> Tuple[Optional[str], Optional[str]]:
        """PURE: the account id — the merchant's own shop — and no address.

        Lets the generic pre-checks refuse a disabled door before anything
        irreversible happens — there is nothing irreversible here yet, but
        the port is the same one the real handshake will use.
        """
        return request.merchant_id, None

    async def gather(self, request: Any) -> OnboardResult:
        """No provider call: report the shop, and no credential.

        An empty ``bundle`` is the honest answer, not a placeholder — the
        token genuinely lives in nautilus. onboarding.py writes the
        installation with a NULL ``credential_id``, and that NULL is what
        routes this shop's actions through the relay.

        ``healthy`` is also honest at this rung: nothing is degraded. There
        is no subscription to have failed and no token to have expired; the
        door is exactly as complete as a relay-era Shopify door can be.
        """
        return OnboardResult(
            external_account_id=request.merchant_id,
            address=None,
            display_label=request.display_label or request.merchant_id,
            bundle={},
            token_expires_at=None,
            health_level="healthy",
        )

    async def revoke(self, bundle: CredentialBundle, external_account_id: str) -> None:
        """Nothing to tell Shopify: we never subscribed to anything, and the
        token we would revoke is not ours to revoke."""
        return None

    async def resubscribe(
        self, bundle: CredentialBundle, external_account_id: str
    ) -> None:
        """Nothing to turn back on — this door has no event stream of its
        own. Shopify letters reach us through the relay's ingest door."""
        return None


__all__ = ["OnboardShopifyRequest", "ShopifyOnboarder"]
