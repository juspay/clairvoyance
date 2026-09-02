"""The door behind a provider account — which installation, and what it unlocks.

Three callers ask the same two questions: send.py once per message,
templates.py once per lifecycle transition, onboarding.py once on disconnect.
Before this file each asked them in its own words — three copies of the
credential sequence, and two definitions of "usable installation" (a bare
``!= "healthy"`` in one file, a named set in another). Two definitions of one
policy is a policy that changes in one place and not the other: the day a
degraded door may register templates but not send, the miss is silent.

So the policy lives here, once, and the answers come back as FACTS or as ONE
domain error. Each caller keeps its own word for the refusal — send.py turns
it into a ``REASON_*`` on the manifest row, templates.py into a
``TemplateError`` behind a 400, disconnect swallows it. That split is the
point rather than an oversight: "no usable credential" is a refusal reason, an
API error and a shrug depending on who asked, and a helper that picked one of
those would be wrong for the other two.

What deliberately does NOT become an AccountError: a database failure. The
vault is read with ``raise_errors=True`` so a pool blip RAISES instead of
reading as "no credential" — the difference between a send that retries and
one that is refused forever. Only a genuine answer (no row, deactivated, would
not decrypt) is a refusal.
"""

from app.crm.connectivity.db.accessors import installation as installation_accessor
from app.crm.connectivity.schemas import ConnectorInstallation, CredentialBundle
from app.database.accessor.breeze_buddy.credentials import get_credential_by_id

#: The only installation state anything may act through — fail closed on
#: everything else, 'connecting' included. Onboarding verifies the token and
#: the endpoint against the provider and writes the row 'healthy' directly, so
#: 'connecting' is an unproven connection with no first-use deadlock to earn it
#: an exception.
USABLE_INSTALLATION_STATES = frozenset({"healthy"})


class AccountError(Exception):
    """No usable door, or no usable credential behind one.

    Messages on this type are written FOR the merchant — each names something
    they can act on (reconnect the account, finish the connection) — so a
    caller that surfaces the text is not leaking an internal detail.
    """


def is_usable(installation: ConnectorInstallation) -> bool:
    """Whether this door may be acted through at all."""
    return installation.status in USABLE_INSTALLATION_STATES


async def healthy_installation(
    merchant_id: str, connector_key: str, provider_account_ref: str
) -> ConnectorInstallation:
    """The door this provider account hangs off, or a refusal.

    Merchant-scoped by the lookup itself, which is what keeps every step
    reached THROUGH it inside one tenant.
    """
    installation = await installation_accessor.get_installation_by_account(
        merchant_id, connector_key, provider_account_ref
    )
    if installation is None:
        raise AccountError(
            f"no connected account '{provider_account_ref}' on this channel"
        )
    if not is_usable(installation):
        raise AccountError(
            f"the account '{provider_account_ref}' is '{installation.status}' — "
            f"reconnect it before using it"
        )
    return installation


async def bundle_for(installation: ConnectorInstallation) -> CredentialBundle:
    """The installation's decrypted secrets, handed over whole.

    Whole, not one key: which secret a face needs is the face's business, and
    generic code picking a key would have to know every provider's bundle.

    Ownership cannot be compared here: the vault's scope column is
    ``reseller_id`` — one level above merchant (migration 022) — and this
    module by law knows no reseller. The guard is the merchant-scoped
    installation the caller arrived with; the onboarding sync that WRITES
    ``credential_id`` owns refusing a foreign reseller's bundle.
    """
    if not installation.credential_id:
        raise AccountError("this connection has no stored credential")
    # mask=False: callers need the real secret, not the API's ****.
    # raise_errors=True: see the module docstring — a blip must raise, not
    # quietly become a permanent refusal.
    credential = await get_credential_by_id(
        installation.credential_id, mask=False, raise_errors=True
    )
    if credential is None or not credential.is_active or not credential.value:
        # Vault row gone, deactivated, or it would not decrypt (an
        # undecryptable value decodes as {}) — one fact to every caller.
        raise AccountError("this connection's credential is missing or unreadable")
    return CredentialBundle(values=credential.value)
