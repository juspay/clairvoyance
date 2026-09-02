"""The connector registry — connector_key, validated against a dict in code.

Canon T11 says `connector_key` is "validated against a dict in code, never by
the table", and until now no such dict existed: the column was free text and
the only real validation was a hardcoded `"whatsapp"` in two logic files.
This is that dict.

It is also the composition root for every NON-send face of a provider
package. `providers/__init__.py` assembles the adapters behind send.py; this
file assembles the onboarders and template faces behind the routes. One root
per face is boundary rule 11, and the reason it is face-precise rather than
folder-precise is a scar: when the rule said "providers/ belongs to send.py",
the onboarding Graph calls were parked in a module-root file to get around
it, and the confined adapter then imported the unconfined one.

Adding a connector is: a package under providers/, one entry here. No branch
anywhere else — onboarding.py and templates.py never name a provider.
"""

from dataclasses import dataclass
from typing import Dict, Optional, Type

from pydantic import BaseModel

from app.crm.connectivity.providers.base import (
    ConnectorHandshakeError,
    ConnectorOnboarder,
)
from app.crm.connectivity.providers.whatsapp.onboard import WhatsappOnboarder
from app.crm.connectivity.schemas import OnboardWhatsappRequest

#: Re-exported so onboarding.py can name the error every onboarder raises.
#: It cannot import providers/base itself — boundary rule 11 gives that file
#: two doors, and this is one of them.
__all__ = [
    "CONNECTORS",
    "ConnectorHandshakeError",
    "ConnectorSpec",
    "connector_for",
]


@dataclass(frozen=True)
class ConnectorSpec:
    """Everything the generic code needs to serve one connector."""

    #: The channel its bindings carry, or None for a connector that does not
    #: SEND. Canon T11's vocabulary is shopify · whatsapp · instagram ·
    #: messenger · zendesk · juspay, and three of those are doors with no
    #: pipe — a Shopify OAuth install is a complete onboarding with nothing
    #: to bind. None keeps them out of the send-side pins and out of the
    #: binding write, instead of forcing a fake channel to satisfy both.
    #:
    #: Separate from connector_key even when set, because the two diverge:
    #: Instagram and Messenger are two connectors on one Meta app.
    channel: Optional[str]
    onboarder: ConnectorOnboarder
    #: The request model the onboard route validates its body against, so the
    #: route itself stays connector-agnostic.
    request_model: Type[BaseModel]


# Instantiated once: the faces are stateless request builders, exactly like
# the adapters.
CONNECTORS: Dict[str, ConnectorSpec] = {
    "whatsapp": ConnectorSpec(
        channel="whatsapp",
        onboarder=WhatsappOnboarder(),
        request_model=OnboardWhatsappRequest,
    ),
}


def connector_for(connector_key: str) -> Optional[ConnectorSpec]:
    """The spec for ``connector_key``, or None.

    None rather than a raise: an unknown key is a 404 at the route, because
    this dict IS the vocabulary — asking for a connector that is not in it is
    asking for something that does not exist, not an error in the request.
    """
    return CONNECTORS.get(connector_key)


def sending_connectors() -> Dict[str, ConnectorSpec]:
    """Only the connectors that carry a channel.

    What the send-side pins may assert against: a data connector has no
    adapter and no entry in CHANNELS, and asserting over the whole registry
    would make adding one fail two tests that are about sending.
    """
    return {key: spec for key, spec in CONNECTORS.items() if spec.channel is not None}
