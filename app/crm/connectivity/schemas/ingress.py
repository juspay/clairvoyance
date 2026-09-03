"""The shapes crossing the ingress seam — no table family, one port.

ProviderLetter is what every provider's inbound face yields: a fact with
its owner still a provider identifier. Neutral by design, so the ingress
root's owner resolution stays generic and no provider name leaks out of
its package.
"""

from datetime import datetime
from typing import Any, Dict, NamedTuple, Optional

#: How an inbound letter names its owner: the receiving ENDPOINT (a phone
#: number id, a sender id, a from-address — whatever the channel's binding
#: stores as its address) or the provider ACCOUNT itself (a WABA, an
#: aggregator account). These are the two lookups the ingress root knows
#: how to resolve — and the ONLY thing it needs to know about any provider.
OWNER_ENDPOINT = "endpoint"
OWNER_ACCOUNT = "account"


class ProviderLetter(NamedTuple):
    """One fact a provider told us, its owner still a provider identifier.

    Three words, three jobs — they coincide for Meta and diverge for the
    next provider, so each is named for what it is: ``source`` is the spine
    word (T13 col 3, the extractor's key — the vendor's shape); ``channel``
    is what an ENDPOINT owner is looked up by (the binding's channel, T12);
    ``connector_key`` is what an ACCOUNT owner is looked up by (the
    installation's connector, T11). An SMS aggregator's face would say
    source "msg91", channel "sms", connector_key "msg91"; Meta says
    "whatsapp" three times.

    ``payload`` is the provider's own object, narrowed to the one item where
    they batch (canon T13 col 7); ``schema_version`` is the provider's API
    version, stamped by the face that knows it.
    """

    owner_kind: str
    owner_id: str
    source: str
    channel: str
    connector_key: str
    topic: str
    external_id: str
    payload: Dict[str, Any]
    occurred_at: Optional[datetime]
    schema_version: str
