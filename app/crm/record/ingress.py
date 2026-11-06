"""The INGRESS slot — provider webhook bays record OWNS but never fills.

design/ingest-doors (amended 2 Sep 2026): every ingest door is record's,
and the provider bays are one registry — GET·POST /ingest/webhooks/
{provider} in record/api.py dispatches through the specs registered here.
But record may import no other module (boundary rule 12), and a provider's
mechanics — its signature ritual, its envelope walk, the owner lookups that
name the merchant — live behind another module's contracts. So the
dependency INVERTS, exactly as it does for spine consumers
(record/consumers.py): the owning module builds an IngressSpec and
app/crm/api.py registers it here, one line, the same line worker_main
writes for consumers. Record knows no provider by name, and
record -> registrant never happens, so the cycle rule 12 exists to kill
cannot form.
"""

from typing import Awaitable, Callable, Dict, List, Mapping, NamedTuple, Optional

from app.crm.record.schemas import EventIn


class IngressSpec(NamedTuple):
    """One provider's bay: how they prove themselves, how their body becomes
    letters, and (only where the provider demands one) how their
    subscription challenge is answered.

    ``verify`` runs over the RAW bytes before any parse. ``envelope``
    receives the parsed body and returns record's letters with the merchant
    already resolved — envelope fields only, never a reading of the
    payload's contents (a semantic problem is quarantine's job, not the
    door's).

    ``verify`` sees the body and the headers — enough for a provider that
    signs with ONE platform secret (Meta: one app, one secret). Named
    trigger, not built ahead of its provider: the first provider whose
    signing secret is PER MERCHANT (a per-account aggregator key) adds a
    bay token to the path — ``/ingest/webhooks/{provider}/{bay}`` — and
    ``verify`` gains the request's path params, so the credential can be
    found before the signature is checked.
    """

    verify: Callable[[bytes, Mapping[str, str]], bool]
    envelope: Callable[[Mapping[str, str], Dict], Awaitable[List[EventIn]]]
    challenge: Callable[[Mapping[str, str]], Optional[str]]


#: The slot. Empty until app/crm/api.py fills it at wiring time — a closed
#: map nobody extends by accident, and an unknown {provider} is a 404.
INGRESS: Dict[str, IngressSpec] = {}


def register_ingress(provider: str, spec: IngressSpec) -> None:
    """Called from the composition root (app/crm/api.py), never from record.

    Last registration wins, which lets a test stand in a fake spec without
    a teardown ritual.
    """
    INGRESS[provider] = spec
