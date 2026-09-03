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
one permission-adjacent lookup belongs in one auditable place.

Why record does not do it instead: record may import no other module
(boundary rule 12), so app/crm/api.py registers each spec into record's
INGRESS slot — the same line worker_main writes for consumers — and record
dispatches knowing no provider by name. Rule 11 makes this file the one
reader of every ``providers/<x>/inbound.py`` face.
"""

from typing import Awaitable, Callable, Dict, List, Mapping, Optional

import app.crm.connectivity.providers.meta.inbound as meta
from app.core.logger import logger
from app.crm.connectivity.db.accessors import (
    binding as binding_accessor,
    installation as installation_accessor,
)
from app.crm.connectivity.schemas.ingress import OWNER_ENDPOINT, ProviderLetter
from app.crm.record.contracts import EventIn, IngressSpec


async def _merchant_for(letter: ProviderLetter) -> Optional[str]:
    """Whose letter this is — the one permission-adjacent lookup, one place.

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
        return binding.merchant_id if binding else None
    installation = await installation_accessor.get_installation_for_inbound(
        letter.connector_key, letter.owner_id
    )
    return installation.merchant_id if installation else None


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
    merchants: Dict[tuple, Optional[str]] = {}
    for letter in letters:
        key = _owner_key(letter)
        if key not in merchants:
            merchants[key] = await _merchant_for(letter)

    out: List[EventIn] = []
    for letter in letters:
        merchant_id = merchants[_owner_key(letter)]
        if merchant_id is None:
            logger.warning(
                f"ingress: {letter.source} webhook for a "
                f"{letter.owner_kind} no merchant owns"
            )
            continue
        out.append(
            EventIn(
                merchant_id=merchant_id,
                source=letter.source,
                topic=letter.topic,
                external_id=letter.external_id,
                payload=letter.payload,
                occurred_at=letter.occurred_at,
                schema_version=letter.schema_version,
            )
        )
    return out


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
