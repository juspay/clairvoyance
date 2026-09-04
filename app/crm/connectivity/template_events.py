"""What a provider DECIDED about a template, applied to the registry — the
spine consumer, and the only writer of provider-owned template state.

Nothing in this module learns a template's fate on a clock. A provider
pushes approvals, rejections, pauses, deletions, re-categorisations and
quality reads as webhooks; the Meta bay files them to the spine
(record/ingress.py, connectivity/ingress.py), and this consumer is what
turns a filed letter into a row change. The periodic full sync that used to
do it was removed before it ever ran: at a thousand merchants it would have
rewritten the same rows twenty-three hours out of twenty-four for no
information, and every drift it could have caught arrives here as an event
anyway.

**Generic, like every other file at this level.** It dispatches through
CONNECTORS and asks the connector's own face to translate the payload
(``TemplateProvider.normalize_event``); Meta's field names and Meta's
SHOUTING are spent inside that package and never reach this one. Rule 11
would forbid the import even if it were tempting.

**The row is found before the provider account is known.** A template
letter's payload names a template, never the WABA it lives under — Meta
puts the account in the envelope, and the bay stores their value verbatim
(canon T13 col 7). So the lookup runs the other way: the provider's id is
globally unique, the row it finds carries its own account and channel, and
those become the CAS predicates the write is guarded by. Nothing here parses
an id out of the external_id the bay composed.

**Registration** is one line in app/crm/worker_main.py, the composition root
— record owns WHEN a consumer runs (per row, inside that row's savepoint,
before its stamp), this module owns WHAT it does, and the import arrow never
points record -> connectivity.

A raise here leaves the letter pending and it returns next poll, so genuine
failures (a database blip) are retried and eventually quarantined by the
pass. Everything this consumer decides NOT to act on returns quietly
instead: a letter about a template we do not hold is an ordinary outcome,
not an error.
"""

from typing import Any, Dict, Optional

from app.core.logger import logger
from app.crm.connectivity.connectors import ConnectorSpec, connector_for_source
from app.crm.connectivity.db.accessors import (
    installation as installation_accessor,
    template as template_accessor,
)
from app.crm.connectivity.schemas.template import ProviderTemplateState, TemplateRead
from app.crm.connectivity.topics import (
    TEMPLATE_TOPICS,
    TOPIC_TEMPLATE_CATEGORY,
    TOPIC_TEMPLATE_QUALITY,
    TOPIC_TEMPLATE_STATUS,
)
from app.crm.record.contracts import RawEvent


async def consume_template_event(
    event: RawEvent,
    customer_id: Optional[str],
    handles: Optional[Dict[str, str]] = None,
    variables: Optional[Dict[str, Any]] = None,
) -> None:
    """One filed letter -> at most one registry row change.

    ``customer_id`` is None for these letters and that is the point: a
    template review names no person by design (canon T13 col 14), and this
    consumer is exactly who a merchant-level letter is for. It is ignored
    rather than tested — every consumer hears every letter, and the topic is
    what makes one ours.

    ``handles`` and ``variables`` are ignored for the same reason and are
    accepted only to satisfy the registry's shape: they describe the PERSON
    a letter is about and the fill-ins a send would render, and a template
    review has neither. The provider's own words in the payload are the
    whole content of these letters.
    """
    if event.topic not in TEMPLATE_TOPICS:
        return

    spec = connector_for_source(event.source)
    if spec is None:
        # A source no connector claims. The same bay serves products we do
        # not register templates for, so this is ordinary.
        logger.debug(
            f"template events: no connector serves source '{event.source}' "
            f"(event {event.id})"
        )
        return

    state = spec.templates.normalize_event(event.topic, event.payload)
    if state is None:
        # The face read the letter and found nothing this registry stores.
        return

    if not state.provider_template_id:
        # Nothing to match on. A letter with no template id is either an
        # account-level notice mis-filed under a template topic or a shape
        # this provider's face could not read; either way it names no row.
        logger.debug(
            f"template events: {event.topic} carries no provider template id "
            f"(event {event.id})"
        )
        return

    template = await template_accessor.get_template_by_provider_id(
        event.merchant_id, state.provider_template_id
    )
    if template is None:
        # An id we have never seen. Either a template registered outside
        # this registry — ordinary, drop it — or the one case worth
        # repairing, which _resume_crashed_submit owns end to end.
        await _resume_crashed_submit(event, spec, state)
        return

    applied = await _apply(event, template, state)
    if applied is None:
        # The guarded write declined: this letter is older than the state
        # the row already carries, or the row moved while it was in flight.
        # Not an error — the guard doing its job — but worth seeing, because
        # a provider that reorders often is a fact about the provider.
        logger.info(
            f"template events: {event.topic} for template {template.id} was "
            f"not applied — the row carries a newer state (event {event.id})"
        )
        return
    logger.info(
        f"template events: {event.topic} applied to template {applied.id} "
        f"({applied.channel}/{applied.name}) — status '{applied.status}', "
        f"category '{applied.category}', quality '{applied.quality}'"
    )


# ---------------------------------------------------------------------------
# The crashed-submit repair
# ---------------------------------------------------------------------------


async def _resume_crashed_submit(
    event: RawEvent, spec: ConnectorSpec, state: ProviderTemplateState
) -> None:
    """Stamp the provider's id onto the row whose submit never recorded it.

    Owns this letter end to end — the resume statement has to write the id
    and the status together, so there is nothing left for the caller to
    apply and nothing useful to hand back.

    Only a STATUS letter may do this: the resume records a status as well as
    an id, and a category or quality letter carries none. A category change
    for a template we cannot match is simply dropped — the status letter for
    the same template repairs the row, and the provider re-sends category on
    every change.

    Matched on the FULL natural key, account included. Matching without the
    account looks sufficient — one merchant, one crashed submit of that name
    and language — and is not: a merchant with two WABAs can hold the claim
    on the first while this letter arrives from the second about a template
    we have never seen (registered in Meta's own console, say). That is
    exactly one candidate and exactly the wrong one, and stamping a globally
    unique provider id onto it is not a mistake anything downstream can
    detect. So the account is resolved first, and no account means no
    resume.
    """
    if event.topic != TOPIC_TEMPLATE_STATUS or not state.status:
        return
    if not (state.name and state.language and spec.channel):
        return

    account = await _account_the_letter_arrived_through(event, spec)
    if account is None:
        return

    claimed = await template_accessor.submitting_template_by_natural_key(
        event.merchant_id, spec.channel, account, state.name, state.language
    )
    if claimed is None:
        # No unconfirmed claim under that key: the template was registered
        # outside this registry, or already retired. Ordinary.
        return

    resumed = await template_accessor.resume_submitted_template(
        event.merchant_id,
        claimed.id,
        claimed.provider_account_ref,
        state.provider_template_id or "",
        state.status,
        event.occurred_at,
        state.rejection_reason,
    )
    if resumed is None:
        # The claim was gone by the time this landed — another letter
        # resumed it, or the submit that looked crashed actually completed.
        logger.info(
            f"template events: the submit claim on template {claimed.id} was "
            f"already resolved (event {event.id})"
        )
        return
    logger.warning(
        f"template events: resumed a crashed submit — template {resumed.id} "
        f"('{resumed.name}'/{resumed.language}) now carries the provider id "
        f"its submission never recorded"
    )


async def _account_the_letter_arrived_through(
    event: RawEvent, spec: ConnectorSpec
) -> Optional[str]:
    """The provider account this letter came in on, or None if it cannot be
    known from a filed letter.

    The letter itself cannot answer this. Meta names the account in the
    ENVELOPE (``entry.id``) and the bay files their ``value`` verbatim
    (canon T13 col 7), and record's letter has nowhere to carry it — T13's
    columns are merchant, source, topic, version, external_id, payload and
    the two clocks, with no owner among them. Reading it back out of the
    external_id the bay composed would couple this file to that
    composition, which rule 11 puts out of reach anyway.

    But it is still DERIVABLE, because the ingress root already resolved
    the merchant BY the account (installation_for_inbound_query): a filed
    letter provably arrived through one of this merchant's non-revoked
    installations on this connector. One such installation, and its
    external_account_id IS the letter's account — known, not guessed.
    Several, and it is genuinely unknowable, so the resume declines. That
    is a real narrowing (a two-WABA merchant loses the automatic repair)
    and the correct one: the alternative is a silent cross-account stamp,
    and the repair is still reachable by resubmitting the draft.
    """
    accounts = await installation_accessor.accounts_for_inbound(
        event.merchant_id, spec.key
    )
    if len(accounts) == 1:
        return accounts[0].external_account_id
    logger.warning(
        f"template events: cannot resume a crashed submit for merchant "
        f"{event.merchant_id} — the letter names no account and the merchant "
        f"holds {len(accounts)} '{spec.key}' accounts, so which one sent it "
        f"is unknowable (event {event.id})"
    )
    return None


# ---------------------------------------------------------------------------
# Applying it
# ---------------------------------------------------------------------------


async def _apply(
    event: RawEvent, template: TemplateRead, state: ProviderTemplateState
) -> Optional[TemplateRead]:
    """The one write, chosen by topic. None means the guard declined."""
    if event.topic == TOPIC_TEMPLATE_STATUS:
        if not state.status:
            return None
        return await template_accessor.apply_status_event(
            event.merchant_id,
            template.id,
            template.provider_account_ref,
            state.status,
            event.occurred_at,
            state.rejection_reason,
        )
    if event.topic == TOPIC_TEMPLATE_CATEGORY:
        if not state.category:
            return None
        return await template_accessor.apply_category_event(
            event.merchant_id,
            template.id,
            template.provider_account_ref,
            state.category,
            event.occurred_at,
        )
    if event.topic == TOPIC_TEMPLATE_QUALITY:
        if not state.quality:
            return None
        return await template_accessor.apply_quality_event(
            event.merchant_id,
            template.id,
            template.provider_account_ref,
            state.quality,
            event.occurred_at,
        )
    return None
