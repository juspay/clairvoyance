"""The ingress root — provider bays assembled for record's webhook door.

GENERIC on purpose: every inbound face yields the same neutral shape
(schemas.ProviderLetter), so the owner-to-merchant resolution below works
for any provider unchanged. Provider names appear only at the bottom: one
spec per provider, exactly like ADAPTERS and CONNECTORS — and, for a
per-account bay (Xyne), the verify that finds that account's secret. Everything provider-specific (wire
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

from typing import Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

import app.crm.connectivity.providers.meta.inbound as meta
import app.crm.connectivity.providers.xyne.inbound as xyne
from app.core.logger import logger
from app.crm.connectivity.accounts import installations
from app.crm.connectivity.db.accessors import (
    binding as binding_accessor,
    installation as installation_accessor,
)
from app.crm.connectivity.schemas.ingress import OWNER_ENDPOINT, ProviderLetter
from app.crm.record.contracts import BayIngressSpec, EventIn, IngressSpec


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
        out.append(_event_in(letter, merchant_id))
    return out


def _event_in(letter: ProviderLetter, merchant_id: str) -> EventIn:
    """A provider letter as record's letter, filed under ``merchant_id``."""
    return EventIn(
        merchant_id=merchant_id,
        source=letter.source,
        topic=letter.topic,
        external_id=letter.external_id,
        payload=letter.payload,
        occurred_at=letter.occurred_at,
        schema_version=letter.schema_version,
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

#: Each inbound face's answer to "where did you file this message's status
#: letters?", asked in turn by channel — a second provider is one more entry.
_STATUS_LETTER_KEYS = (meta.status_letter_keys,)


def status_letter_keys(
    channel: str, provider_message_id: str, states: Iterable[str]
) -> Optional[Tuple[str, List[str]]]:
    """Where a message's status letters were filed — (spine source, external
    ids) — answered by the face that files them; None when no face serves
    the channel. The receipts code reads a message's receipts back through
    this, never by composing a provider's ids itself (rule 11)."""
    wanted = list(states)
    for keys_of in _STATUS_LETTER_KEYS:
        found = keys_of(channel, provider_message_id, wanted)
        if found is not None:
            return found
    return None


async def _xyne_verify(
    app_id: str, raw_body: bytes, headers: Mapping[str, str]
) -> Optional[str]:
    """This app's secret, found by the app id in the URL, checks these bytes;
    the answer is the merchant whose connection that secret came from.

    That merchant is the one the envelope files under — one lookup for both
    halves, so the connection that verified a callback is the one that owns
    it. Fail closed at every step: an app nobody holds, a revoked or
    disabled connection, a credential that will not load, or a bundle
    without a signing secret all answer None — the same 403 as a wrong
    signature. Only a read that fails outright (the database) raises, and
    the bay answers that with its failure reply.
    """
    installation = await installation_accessor.get_installation_for_inbound(
        xyne.CONNECTOR_KEY, app_id
    )
    if installation is None or not installations.is_usable(installation):
        logger.warning("ingress: xyne callback for an app with no usable connection")
        return None
    try:
        bundle = await installations.bundle_for(installation)
    except installations.AccountError as e:
        logger.warning(f"ingress: xyne app credential unusable — {e}")
        return None
    if not xyne.verify_signature(
        raw_body, headers, bundle.secret(xyne.WEBHOOK_SECRET_KEY)
    ):
        return None
    return installation.merchant_id


async def _xyne_envelope(
    app_id: str, merchant_id: str, headers: Mapping[str, str], body: Dict
) -> List[EventIn]:
    """Parse (the face's), then file under the merchant verify proved — no
    second owner lookup, so nothing can change between the two."""
    return [
        _event_in(letter, merchant_id) for letter in xyne.letters(body, headers, app_id)
    ]


#: Registered by app/crm/api.py as a PER-APP bay:
#: POST /ingest/webhooks/xyne/{app_id}. Xyne waits up to 30 s for an
#: AppActionResponse and shows it in the chat, and never retries — so the
#: bay answers in its words: the form closes on a stored answer, a plain ack
#: for what we do not file (a field edit), and on a failure a 503 with the
#: typed error Xyne renders to the person, who can submit again
#: (flowController.ts: a non-2xx with {"type":"error"} is shown as sent).
XYNE_INGRESS = BayIngressSpec(
    verify=_xyne_verify,
    envelope=_xyne_envelope,
    stored_reply={"type": "close_screen", "finalMessage": "Response received"},
    empty_reply={"type": "ack"},
    failed_reply={
        "type": "error",
        "message": "Couldn't save your response, please submit again",
        "retryable": True,
    },
)
