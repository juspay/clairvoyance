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
    ProviderError,
    TemplateProvider,
    TemplateProviderError,
)
from app.crm.connectivity.providers.whatsapp.onboard import (
    OnboardWhatsappRequest,
    WhatsappOnboarder,
)
from app.crm.connectivity.providers.whatsapp.templates import WhatsappTemplates

#: Re-exported so onboarding.py can name the error every onboarder raises.
#: It cannot import providers/base itself — boundary rule 11 gives that file
#: two doors, and this is one of them.
__all__ = [
    "CONNECTORS",
    "ConnectorHandshakeError",
    "ConnectorSpec",
    "ProviderError",
    "TemplateProviderError",
    "connector_for",
    "connector_for_channel",
    "connector_for_source",
    "sending_connectors",
]


@dataclass(frozen=True)
class ConnectorSpec:
    """Everything the generic code needs to serve one connector."""

    #: The registry key this spec is filed under.
    key: str

    #: The SPINE word for the letters this connector's provider files (T13
    #: col 3, the extractor's key). Third of the three words ProviderLetter
    #: already names, and here for the same reason it is named there: they
    #: coincide for Meta ("whatsapp" three times) and diverge for the next
    #: provider, whose face would say source "msg91", channel "sms".
    #:
    #: A filed letter keeps only its source — EventIn has no room for the
    #: other two — so the spine consumer has nothing else to dispatch on.
    #: Without this field it would have to ASSUME source == key, which is
    #: the "two parallel answers" scar channels.py opens with; with it, the
    #: day the two diverge is one line in this dict.
    source: str

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
    templates: TemplateProvider
    #: The request model the onboard route validates its body against, so the
    #: route itself stays connector-agnostic.
    request_model: Type[BaseModel]


# Instantiated once: the faces are stateless request builders, exactly like
# the adapters.
CONNECTORS: Dict[str, ConnectorSpec] = {
    "whatsapp": ConnectorSpec(
        key="whatsapp",
        source="whatsapp",
        channel="whatsapp",
        onboarder=WhatsappOnboarder(),
        templates=WhatsappTemplates(),
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


def connector_for_channel(channel: str) -> Optional[ConnectorSpec]:
    """The spec serving ``channel``, or None.

    A template row carries a channel, not a connector key, so the template
    lifecycle resolves its provider this way. Linear over a dict of a handful
    of entries: a second index would be state to keep consistent for a lookup
    that is never hot.

    The falsy guard matters now that a spec's channel may be None: without it
    a lookup for "no channel" would match a data connector — a door with no
    pipe — and the template lifecycle would try to register a message shape
    against something that cannot send one.
    """
    if not channel:
        return None
    for spec in CONNECTORS.values():
        if spec.channel == channel:
            return spec
    return None


def connector_for_source(source: str) -> Optional[ConnectorSpec]:
    """The spec whose provider files letters under ``source``, or None.

    The spine consumer's lookup. A filed letter carries the source word and
    nothing else of the three — record's letters have no room for a channel
    or a connector key — so this is how a consumer finds the face that can
    read the payload it is holding.

    None rather than a raise, and the caller returns: a letter from a source
    no connector claims is not this module's business, and the same bay
    delivers products we do not serve yet.
    """
    if not source:
        return None
    for spec in CONNECTORS.values():
        if spec.source == source:
            return spec
    return None
