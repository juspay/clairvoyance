"""The ingress root — provider bays assembled for record's webhook door.

GENERIC on purpose: every inbound face yields the same neutral shape
(schemas.ProviderLetter), so the owner-to-merchant resolution below works
for any provider unchanged. The ONLY provider names in this file are the
spec assemblies at the bottom — one registry-style entry per provider,
exactly like ADAPTERS and CONNECTORS. Everything provider-specific (wire
shape, signature, topics, versions) lives in that provider's face under
providers/.

Why the resolution lives here and not in the face: provider files talk to
the provider and never to our tables (the base.py port contract — a face
"returns FACTS; it writes nothing"). Deciding WHOSE letter something is
means reading crm_channel_binding / crm_connector_installation, and that
one permission-adjacent lookup belongs in one auditable place. The same
lookup answers the door's traffic heartbeat, which is the one write this
path makes beside the letter itself (_stamp_traffic says why it earns it).

Why record does not do it instead: record may import no other module
(boundary rule 12), so app/crm/api.py registers each spec into record's
INGRESS slot — the same line worker_main writes for consumers — and record
dispatches knowing no provider by name. Rule 11 makes this file the one
reader of every ``providers/<x>/inbound.py`` face.
"""

from typing import (
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
)

import app.crm.connectivity.providers.meta.inbound as meta
from app.core.logger import logger
from app.crm.connectivity.db.accessors import (
    binding as binding_accessor,
    installation as installation_accessor,
)
from app.crm.connectivity.schemas.ingress import OWNER_ENDPOINT, ProviderLetter
from app.crm.record.contracts import EventIn, IngressSpec


class _Owner(NamedTuple):
    """Whose letter this is, and which DOOR it came through.

    Two facts from one lookup because both callers of that lookup need a
    different half: filing needs the merchant, and the door's traffic
    heartbeat needs the installation. Resolving twice would double the
    cost of the one permission-adjacent read on the inbound path.
    """

    merchant_id: str
    installation_id: str


async def _owner_for(letter: ProviderLetter) -> Optional[_Owner]:
    """The one permission-adjacent lookup, one place.

    A receiving endpoint resolves through its binding, keyed by the
    letter's CHANNEL; a provider account resolves through its
    installation, keyed by the letter's CONNECTOR KEY. Never by ``source``
    — that is the spine word, and the three coincide only for Meta. None
    means nobody owns it, and the caller drops the letter (filing it
    under any merchant would be a cross-tenant leak).
    """
    if letter.owner_kind == OWNER_ENDPOINT:
        binding = await binding_accessor.get_binding_for_inbound(
            letter.channel, letter.owner_id
        )
        if binding is None:
            return None
        return _Owner(binding.merchant_id, binding.installation_id)
    installation = await installation_accessor.get_installation_for_inbound(
        letter.connector_key, letter.owner_id
    )
    if installation is None:
        return None
    return _Owner(installation.merchant_id, installation.id)


def _owner_key(letter: ProviderLetter) -> tuple:
    """PURE: what makes two letters share one owner lookup."""
    scope = (
        letter.channel if letter.owner_kind == OWNER_ENDPOINT else letter.connector_key
    )
    return (letter.owner_kind, scope, letter.owner_id)


async def resolve_letters(letters: List[ProviderLetter]) -> List[EventIn]:
    """Provider letters -> record's letters, owners resolved to merchants.

    Grouped by owner first: one callback may legitimately carry facts for
    several merchants, so each owner costs one lookup — and an owner nobody
    holds must not cost the letters beside it, which belong to someone else
    entirely.
    """
    owners: Dict[tuple, Optional[_Owner]] = {}
    for letter in letters:
        key = _owner_key(letter)
        if key not in owners:
            owners[key] = await _owner_for(letter)
    await _stamp_traffic(owners.values())

    out: List[EventIn] = []
    for letter in letters:
        owner = owners[_owner_key(letter)]
        if owner is None:
            logger.warning(
                f"ingress: {letter.source} webhook for a "
                f"{letter.owner_kind} no merchant owns"
            )
            continue
        out.append(
            EventIn(
                merchant_id=owner.merchant_id,
                source=letter.source,
                topic=letter.topic,
                external_id=letter.external_id,
                payload=letter.payload,
                occurred_at=letter.occurred_at,
                schema_version=letter.schema_version,
            )
        )
    return out


async def _stamp_traffic(owners: Iterable[Optional[_Owner]]) -> None:
    """The doors these letters arrived through heard from their provider —
    canon T11 col 10's heartbeat, once per door per callback.

    This is the ONLY thing the bay writes that is not the letter itself, and
    it earns the exception: the column's whole job is to catch the failure
    no probe can fake — a token still valid, the connection still green, and
    the webhook subscription silently gone — which is only visible as this
    stamp ceasing to advance. Stamping it from the template consumer instead
    would make the column lie: a busy account can go months between template
    events while message traffic pours in.

    Deliberately NOT fatal. A letter must be filed whether or not its door's
    bookkeeping lands; failing the callback over a heartbeat would make Meta
    retry a letter we already understood, and the retry would fail the same
    way.
    """
    for owner in {o.installation_id: o for o in owners if o is not None}.values():
        try:
            await installation_accessor.stamp_last_event_at(
                owner.merchant_id, owner.installation_id
            )
        except Exception as e:
            logger.opt(exception=e).warning(
                f"ingress: could not stamp last_event_at on installation "
                f"{owner.installation_id}"
            )


def envelope_over(
    letters_of: Callable[[Dict], List[ProviderLetter]],
) -> Callable[[Mapping[str, str], Dict], Awaitable[List[EventIn]]]:
    """An IngressSpec envelope from a face's ``letters`` verb — the generic
    half applied to any provider's parse."""

    async def _envelope(headers: Mapping[str, str], body: Dict) -> List[EventIn]:
        """The registered envelope: parse (the face's), resolve (ours)."""
        return await resolve_letters(letters_of(body))

    return _envelope


# --- the bays: one assembly per provider (the only provider names here) ------

#: Registered by app/crm/api.py under the key "meta" — the PROVIDER, not a
#: channel: one Meta app serves WhatsApp, Instagram and Messenger through
#: one callback, and each letter carries its own source.
META_INGRESS = IngressSpec(
    verify=meta.verify_signature,
    envelope=envelope_over(meta.letters),
    challenge=meta.handshake_challenge,
)
