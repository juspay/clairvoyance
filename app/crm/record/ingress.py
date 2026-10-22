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
    signs with ONE platform secret (Meta: one app, one secret). A provider
    whose signing secret is PER ACCOUNT registers a BayIngressSpec instead.
    The door answers with an empty 200, or a 503 on a store failure — the
    answers a provider that retries on its own understands.
    """

    verify: Callable[[bytes, Mapping[str, str]], bool]
    envelope: Callable[[Mapping[str, str], Dict], Awaitable[List[EventIn]]]
    challenge: Callable[[Mapping[str, str]], Optional[str]]


class BayIngressSpec(NamedTuple):
    """A provider whose signing secret is PER ACCOUNT: the bay token in the
    path — ``/ingest/webhooks/{provider}/{bay}`` — names the account, so the
    credential can be found before the signature is checked.

    ``verify`` is async because finding that credential is a read; it gets
    the bay first and answers the MERCHANT whose door signed these bytes, or
    None for a bay nobody holds, exactly as for a wrong signature.
    ``envelope`` is handed the bay AND that merchant id: the letters are
    filed under the door that verified them and never re-resolved, so a
    re-onboard between the two steps cannot verify with one merchant's
    secret and file under another's. The owner is the account the URL
    named — never something the body claims. No ``challenge``: a bayed
    provider with a subscription handshake can add one the day it exists.

    ``success_reply`` / ``error_reply`` are the door's ANSWER BODY when set.
    Unset, the door keeps the shared door's empty 200 and its
    503-dogged-by-dedupe on a store failure. A provider that shows the answer
    to a person and never retries (Xyne returns an AppActionResponse in the
    chat) sets both: ``error_reply`` goes back as a 200 with the error JSON,
    never a 503 — a 503 tells a non-retrying sender to give up, while the
    error object is what the person sees and resubmits from.

    ``ignored`` picks out the signed bodies the provider sends that we
    deliberately do not file (a Xyne field-change callback, sent to the same
    URL as the submit). They are answered ``ignored_reply`` — never
    ``error_reply``, which would tell the person something failed on every
    keystroke — and nothing is stored.
    """

    verify: Callable[[str, bytes, Mapping[str, str]], Awaitable[Optional[str]]]
    envelope: Callable[[str, str, Mapping[str, str], Dict], Awaitable[List[EventIn]]]
    success_reply: Optional[Dict] = None
    error_reply: Optional[Dict] = None
    ignored: Optional[Callable[[Dict], bool]] = None
    ignored_reply: Optional[Dict] = None


#: The slot. Empty until app/crm/api.py fills it at wiring time — a closed
#: map nobody extends by accident, and an unknown {provider} is a 404.
INGRESS: Dict[str, IngressSpec] = {}


def register_ingress(provider: str, spec: IngressSpec) -> None:
    """Called from the composition root (app/crm/api.py), never from record.

    Last registration wins, which lets a test stand in a fake spec without
    a teardown ritual.
    """
    INGRESS[provider] = spec


#: The per-account bays, keyed by provider. A separate slot rather than a
#: union in INGRESS: the two door shapes take different arguments, and a
#: provider is one or the other.
BAY_INGRESS: Dict[str, BayIngressSpec] = {}


def register_bay_ingress(provider: str, spec: BayIngressSpec) -> None:
    """register_ingress's twin for per-account bays — same caller, same
    last-wins rule."""
    BAY_INGRESS[provider] = spec
