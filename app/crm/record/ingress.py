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

from typing import Any, Awaitable, Callable, Dict, List, Mapping, NamedTuple, Optional

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
    """

    verify: Callable[[bytes, Mapping[str, str]], bool]
    envelope: Callable[[Mapping[str, str], Dict], Awaitable[List[EventIn]]]
    challenge: Callable[[Mapping[str, str]], Optional[str]]


class BayIngressSpec(NamedTuple):
    """A provider whose signing secret is PER ACCOUNT: the bay token in the
    path — ``/ingest/webhooks/{provider}/{bay}`` — names the account, so the
    credential can be found before the signature is checked.

    ``verify(bay, body, headers)`` is async because finding that credential
    is a read. It answers the MERCHANT whose account signed these bytes, or
    None — for a bay nobody holds exactly as for a wrong signature: the same
    403 either way. ``envelope(bay,
    merchant_id, headers, body)`` files under THAT merchant and never
    resolves it again: a reconnect between the two steps cannot verify with
    one merchant's secret and file under another's.

    The three replies are for a provider that shows our answer to a person
    and does not retry (Xyne renders it in the chat): ``stored_reply`` when
    every letter is filed, ``empty_reply`` when a genuine callback carries
    nothing to file (a field edit, an event we do not keep), and
    ``failed_reply`` WITH the 503 when a read or a store fails — the status a
    retrying sender understands, the body a person can act on. Unset, the
    bay answers like the shared one: an empty 200, a bare 503.

    Corpus trail — a ruling owed: design/ingest-doors.md says "one bay per
    provider", with ``verify(raw_body, headers) -> bool``. This is the
    trigger IngressSpec's docstring named before it existed — the first
    provider whose signing secret is per account (Xyne: one secret per app)
    puts the account in the path, because its body names no account and the
    URL is the only safe place to learn whose callback it is.
    """

    verify: Callable[[str, bytes, Mapping[str, str]], Awaitable[Optional[str]]]
    envelope: Callable[[str, str, Mapping[str, str], Dict], Awaitable[List[EventIn]]]
    stored_reply: Optional[Dict[str, Any]] = None
    empty_reply: Optional[Dict[str, Any]] = None
    failed_reply: Optional[Dict[str, Any]] = None


#: The slot. Empty until app/crm/api.py fills it at wiring time — a closed
#: map nobody extends by accident, and an unknown {provider} is a 404.
INGRESS: Dict[str, IngressSpec] = {}

#: The per-account bays, keyed by provider. A separate slot rather than a
#: union in INGRESS: the two shapes take different arguments, and a provider
#: is one or the other.
BAY_INGRESS: Dict[str, BayIngressSpec] = {}


def register_ingress(provider: str, spec: IngressSpec) -> None:
    """Called from the composition root (app/crm/api.py), never from record.

    Last registration wins, which lets a test stand in a fake spec without
    a teardown ritual.
    """
    INGRESS[provider] = spec


def register_bay_ingress(provider: str, spec: BayIngressSpec) -> None:
    """register_ingress's twin for per-account bays — same caller, same
    last-wins rule."""
    BAY_INGRESS[provider] = spec
