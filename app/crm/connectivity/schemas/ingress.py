"""The shapes crossing the ingress seam — no table family, one port.

ProviderLetter is what every provider's inbound face yields: a fact with
its owner still a provider identifier. Neutral by design, so the ingress
root's owner resolution stays generic and no provider name leaks out of
its package.
"""

from datetime import datetime
from typing import Any, Dict, NamedTuple, Optional

#: How an inbound letter names its owner: the receiving endpoint (a phone
#: number id) or the provider account itself (a WABA). These are the two
#: lookups the ingress root knows how to resolve — and the ONLY thing it
#: needs to know about any provider.
OWNER_PHONE_NUMBER = "phone_number"
OWNER_ACCOUNT = "account"


class ProviderLetter(NamedTuple):
    """One fact a provider told us, its owner still a provider identifier.

    ``payload`` is the provider's own object, narrowed to the one item where
    they batch (canon T13 col 7); ``schema_version`` is the provider's API
    version, stamped by the face that knows it.
    """

    owner_kind: str
    owner_id: str
    source: str
    topic: str
    external_id: str
    payload: Dict[str, Any]
    occurred_at: Optional[datetime]
    schema_version: str
