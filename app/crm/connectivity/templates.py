"""The template registry (T23) — generic, dispatching through CONNECTORS.

canon T23 calls this registry multi-channel: WhatsApp first, SMS-DLT second,
email later. So nothing here names a provider. A template row carries a
`channel`; the registry asks which connector serves that channel and hands
the work to its face, which returns a NORMALISED ProviderTemplateState.

The lifecycle, and where each rule lives:

    create   local only                  draft
    submit   claim -> provider -> record  draft -> submitting -> pending
    edit     draft: local; otherwise      approved/rejected/paused -> pending
             in place at the provider     (only if the provider supports it)
    retire   provider, then local         -> deleted

Two things this file deliberately does NOT do.

**It does not decide what "editable" means for a provider.** Meta re-reviews
an edited template on the same row; SMS-DLT has to re-register under a new
id. The branch reads `provider.edits_in_place`, not a provider's name.

**It does not learn status on a clock.** Providers push status, category and
quality as events, and the webhook consumer (PR C) will be the only writer of
them. A timed full sync across every merchant rewrites the same rows
twenty-three hours out of twenty-four for zero information, and the one it
would catch arrives as a webhook anyway.
"""

from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.crm.connectivity import accounts
from app.crm.connectivity.connectors import (
    ConnectorSpec,
    ProviderError,
    connector_for_channel,
)
from app.crm.connectivity.db import DbTxn, atomically
from app.crm.connectivity.db.accessors import (
    template as template_accessor,
)
from app.crm.connectivity.schemas.connector import ConnectorInstallation
from app.crm.connectivity.schemas.message import CredentialBundle
from app.crm.connectivity.schemas.template import (
    ApprovedTemplate,
    TemplateDraft,
    TemplateRead,
)
from app.crm.connectivity.status import (
    TEMPLATE_APPROVED,
    TEMPLATE_DRAFT,
    TEMPLATE_IN_PLACE_EDIT,
    TEMPLATE_LOCAL_EDIT,
    TEMPLATE_PENDING,
    TEMPLATE_REJECTED,
)


class TemplateError(Exception):
    """A transition is not available from this template's current status, or
    the account behind it cannot be resolved."""


class TemplateNotFoundError(TemplateError):
    """No such template for this merchant — a 404, not a 400."""


def _as_template_error(error: Exception) -> TemplateError:
    """A provider's exception, translated at this module's edge.

    api.py cannot catch a provider's own type — boundary rule 11 forbids it
    from importing a provider package at all — so every failure leaves here
    wearing this module's word for it.

    A DECLARED refusal is passed through verbatim: for a template operation
    the provider's message is the actionable part ("component BODY has too
    many variables"), and what it describes is the merchant's own template.
    Anything else is a bug, and its text is an internal detail — a
    KeyError('id') must not become a 400 whose body reads 'id'. Same split
    onboarding makes with ConnectorHandshakeError.
    """
    if isinstance(error, TemplateError):
        return error
    if isinstance(error, ProviderError):
        # The base, not the template leaf: every face declares its refusals
        # under one type, so a face added later is covered without editing
        # this line — which is the whole reason the base exists.
        return TemplateError(str(error) or "the provider refused this template")
    logger.opt(exception=error).error(
        "templates: a template operation raised unexpectedly"
    )
    return TemplateError("could not complete the template operation")


# ---------------------------------------------------------------------------
# Resolving the provider behind a row
# ---------------------------------------------------------------------------


def _spec_for(channel: str) -> ConnectorSpec:
    spec = connector_for_channel(channel)
    if spec is None:
        raise TemplateError(f"no connector serves channel '{channel}'")
    return spec


async def _healthy_installation(
    merchant_id: str, connector_key: str, provider_account_ref: str
) -> ConnectorInstallation:
    """accounts.py's answer, wearing this module's word for a refusal.

    The policy — which statuses are usable — lives in accounts.py alone, so
    the send door and this one cannot drift apart on it. All that is left here
    is the translation every caller does in its own vocabulary.
    """
    try:
        return await accounts.healthy_installation(
            merchant_id, connector_key, provider_account_ref
        )
    except accounts.AccountError as e:
        raise TemplateError(str(e)) from e


async def _bundle_for(installation: ConnectorInstallation) -> CredentialBundle:
    """The same translation for the credential step."""
    try:
        return await accounts.bundle_for(installation)
    except accounts.AccountError as e:
        raise TemplateError(str(e)) from e


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def create_draft(
    merchant_id: str,
    channel: str,
    provider_account_ref: str,
    name: str,
    language: str,
    components: List[Dict[str, Any]],
) -> TemplateRead:
    """A local draft. Nothing is sent to the provider until submit().

    Both inputs are validated CLOSED here rather than at submit, because a
    draft that cannot ever be submitted is worse than a refusal: it looks
    like progress. An unregistered channel has no provider to submit to, and
    an account with no healthy connection has no credential to submit with.

    Idempotent on the natural key, but only while the row is still a draft —
    law #4's "safely callable twice" does not extend to overwriting the
    components a provider is currently reviewing.
    """
    # Refuses an unregistered channel. The spec's own channel is this one by
    # construction — connector_for_channel matches on equality — so the
    # validated parameter is what gets stored, and it is already a str. A
    # spec's channel is Optional now that a connector may be a door with no
    # pipe, and such a connector can never be resolved from a channel here.
    spec = _spec_for(channel)
    await _healthy_installation(merchant_id, spec.key, provider_account_ref)
    return await atomically(
        _create_draft_in_txn,
        merchant_id,
        channel,
        provider_account_ref,
        name,
        language,
        components,
    )


async def _create_draft_in_txn(
    txn: DbTxn,
    merchant_id: str,
    channel: str,
    provider_account_ref: str,
    name: str,
    language: str,
    components: List[Dict[str, Any]],
) -> TemplateRead:
    """ATOMIC: the natural-key probe and the write share one fate — two
    concurrent create calls for one key must not both pass the probe and
    then meet as a unique violation the caller cannot explain."""
    existing = await template_accessor.get_template_by_natural_key(
        txn, merchant_id, channel, provider_account_ref, name, language
    )
    if existing is None:
        return await template_accessor.insert_template_draft(
            txn, merchant_id, channel, provider_account_ref, name, language, components
        )
    if existing.status != TEMPLATE_DRAFT:
        raise TemplateError(
            f"a template named '{name}' ({language}) already exists on this "
            f"account and is '{existing.status}' — edit it instead of recreating it"
        )
    updated = await template_accessor.update_draft_components(
        txn, merchant_id, existing.id, components
    )
    if updated is None:
        raise TemplateError("the draft changed while it was being updated")
    return updated


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------


async def submit(merchant_id: str, template_id: str, category: str) -> TemplateRead:
    """Register a draft with its provider.

    The shape is claim -> call -> record, and the claim is what stops one
    template being registered twice: two requests that both read 'draft'
    would both POST, and the provider refuses the second by name only AFTER
    we have fired it.

    Everything between the claim and the provider's acceptance is wrapped,
    because all of it can fail — the credential may not resolve, the
    components may be refused, the process may die. A claim left standing
    after a failure is permanent: 'submitting' is not re-claimable (by
    design), and the resume path cannot help because the provider never
    received it. Releasing it is the difference between "try again" and "this
    template is dead".
    """
    template = await template_accessor.get_template(merchant_id, template_id)
    if template is None:
        raise TemplateNotFoundError("no such template")
    if template.status != TEMPLATE_DRAFT:
        raise TemplateError(
            f"this template is '{template.status}' — only a draft can be "
            f"submitted"
            + (
                "; edit it to send the corrected version for review"
                if template.status == TEMPLATE_REJECTED
                else ""
            )
        )

    claimed = await atomically(_claim_for_submit_in_txn, merchant_id, template_id)
    if claimed is None:
        raise TemplateError("this template is already being submitted")

    try:
        spec = _spec_for(claimed.channel)
        installation = await _healthy_installation(
            merchant_id,
            spec.key,
            claimed.provider_account_ref,
        )
        bundle = await _bundle_for(installation)
        state = await spec.templates.submit(
            bundle,
            claimed.provider_account_ref,
            TemplateDraft(
                name=claimed.name,
                language=claimed.language,
                category=category,
                components=claimed.components,
            ),
        )
    except Exception as e:
        # The provider never accepted it, so the row goes back to being a
        # draft the merchant can fix and retry. Re-raised: the caller still
        # needs to see what went wrong.
        released = await template_accessor.release_submit_claim(
            merchant_id, template_id
        )
        if released is None:
            logger.error(
                f"template {template_id}: submit failed and the claim could not "
                f"be released — the row may be stuck in 'submitting'"
            )
        raise _as_template_error(e) from e

    updated = await atomically(
        _record_submission_in_txn,
        merchant_id,
        template_id,
        state.provider_template_id or "",
        state.category,
        category,
        state.status or TEMPLATE_PENDING,
    )
    return updated


async def _claim_for_submit_in_txn(
    txn: DbTxn, merchant_id: str, template_id: str
) -> Optional[TemplateRead]:
    """ATOMIC: the status test and the swap to 'submitting' share one fate —
    two concurrent submits must not both believe they hold the claim, because
    each of them would then register the same template with the provider."""
    return await template_accessor.claim_for_submit(txn, merchant_id, template_id)


async def _record_submission_in_txn(
    txn: DbTxn,
    merchant_id: str,
    template_id: str,
    provider_template_id: str,
    category: Optional[str],
    submitted_category: str,
    status: str,
) -> TemplateRead:
    """ATOMIC: the provider's id, category and status land together — a
    reader must never see a template carrying the provider's id beside our
    stale status, which is exactly the state the webhook consumer's resume
    path is written to repair."""
    updated = await template_accessor.record_submission(
        txn,
        merchant_id,
        template_id,
        provider_template_id,
        category,
        submitted_category,
        status,
    )
    if updated is None:
        raise TemplateError("the template changed while its submission was recorded")
    return updated


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


async def edit(
    merchant_id: str, template_id: str, components: List[Dict[str, Any]]
) -> TemplateRead:
    """Replace a template's components.

    A draft is edited locally. Anything the provider has already seen is
    edited AT the provider — and for a provider that re-reviews in place,
    that edit IS the way to resubmit a rejected template.
    """
    template = await template_accessor.get_template(merchant_id, template_id)
    if template is None:
        raise TemplateNotFoundError("no such template")

    if template.status in TEMPLATE_LOCAL_EDIT:
        return await atomically(
            _edit_draft_in_txn, merchant_id, template_id, components
        )

    spec = _spec_for(template.channel)
    if template.status not in TEMPLATE_IN_PLACE_EDIT:
        raise TemplateError(
            f"this template is '{template.status}' — it cannot be edited from "
            f"that state"
        )
    if not spec.templates.edits_in_place:
        # Honest rather than a 400 carrying another provider's rule: this
        # provider genuinely cannot re-review a registered template.
        raise TemplateError(
            f"templates on '{template.channel}' cannot be edited once "
            f"registered — retire this one and register a new one"
        )
    if template.provider_template_id is None:
        raise TemplateError(
            "this template has no provider id yet — its submission has not "
            "been confirmed"
        )

    installation = await _healthy_installation(
        merchant_id, spec.key, template.provider_account_ref
    )
    bundle = await _bundle_for(installation)
    try:
        state = await spec.templates.edit(
            bundle,
            template.provider_account_ref,
            template.provider_template_id,
            components,
        )
    except Exception as e:
        # Nothing local has changed yet, so there is no claim to release —
        # only the provider's word to translate into ours.
        raise _as_template_error(e) from e
    return await atomically(
        _edit_registered_in_txn,
        merchant_id,
        template_id,
        components,
        state.status or TEMPLATE_PENDING,
        template.status,
    )


async def _edit_draft_in_txn(
    txn: DbTxn, merchant_id: str, template_id: str, components: List[Dict[str, Any]]
) -> TemplateRead:
    """ATOMIC: one statement — this exists so a draft edit enters the
    database through the same door every other transition does, rather than
    growing a second way in the first time it needs a second statement."""
    updated = await template_accessor.update_draft_components(
        txn, merchant_id, template_id, components
    )
    if updated is None:
        raise TemplateError("this template is no longer a draft")
    return updated


async def _edit_registered_in_txn(
    txn: DbTxn,
    merchant_id: str,
    template_id: str,
    components: List[Dict[str, Any]],
    status: str,
    expected_status: str,
) -> TemplateRead:
    """ATOMIC: the new components and the status they put the row back into
    share one fate (canon T23: editing an approved template returns the SAME
    row to pending) — a reader must never see new components still labelled
    'approved', because the send path would take that as permission.

    Conditional on the status this edit was authorised against. The provider
    call happens outside any transaction, so a concurrent retire() can land
    in between; without the guard this write would put fresh components and
    'pending' over a row the merchant just deleted."""
    updated = await template_accessor.record_in_place_edit(
        txn, merchant_id, template_id, components, status, expected_status
    )
    if updated is None:
        raise TemplateError(
            "the template changed while it was being edited — reload it and "
            "try again"
        )
    return updated


# ---------------------------------------------------------------------------
# retire
# ---------------------------------------------------------------------------


async def retire(merchant_id: str, template_id: str) -> TemplateRead:
    """Withdraw a template, at the provider and here.

    The provider call is best-effort: a provider outage must not leave a
    merchant unable to stop using a template locally. Local retirement is
    what the send path reads, so it is the one that has to happen.

    A 'submitting' row can be retired mid-flight, and the submit that is
    still in the air then finds no claim to record against and fails. That
    is acceptable rather than guarded: the merchant asked for this template
    to be gone, the provider's own status webhook still arrives, and the
    resume path matches by natural key rather than by our claim.
    """
    template = await template_accessor.get_template(merchant_id, template_id)
    if template is None:
        raise TemplateNotFoundError("no such template")

    if template.provider_template_id is not None:
        try:
            spec = _spec_for(template.channel)
            installation = await _healthy_installation(
                merchant_id,
                spec.key,
                template.provider_account_ref,
            )
            bundle = await _bundle_for(installation)
            await spec.templates.retire(
                bundle,
                template.provider_account_ref,
                template.provider_template_id,
                template.name,
                template.language,
            )
        except Exception as e:
            logger.opt(exception=e).warning(
                f"template {template_id}: could not withdraw it at the provider, "
                f"retiring locally anyway"
            )

    updated = await atomically(_retire_in_txn, merchant_id, template_id)
    if updated is None:
        raise TemplateNotFoundError("no such template")
    return updated


async def _retire_in_txn(
    txn: DbTxn, merchant_id: str, template_id: str
) -> Optional[TemplateRead]:
    """ATOMIC: one statement — the same single door every transition uses."""
    return await template_accessor.retire_template(txn, merchant_id, template_id)


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


async def get(merchant_id: str, template_id: str) -> Optional[TemplateRead]:
    return await template_accessor.get_template(merchant_id, template_id)


async def list_templates(
    merchant_id: str, channel: Optional[str] = None, status: Optional[str] = None
) -> List[TemplateRead]:
    return await template_accessor.list_templates(merchant_id, channel, status)


async def template_status(merchant_id: str, channel: str, name: str) -> Optional[str]:
    """Is this template NAME publishable on this channel — the registry's
    one publish-time read (rollout phase 08, G12), beside approved_template
    (the send-time read) so every read of the table stays in this file.

    A workflow's send node names a template and a channel, never the
    provider account that will serve it (the route picks that at send
    time), so the question is asked across the merchant's accounts:

      * None — no row under that name: never registered here;
      * "approved" — every account holding the name holds exactly ONE
        approved row (the send door will find its one row whichever
        account the route picks);
      * "approved in N languages" — some account holds several approved
        rows: the ambiguity approved_template refuses at send time,
        refused here first — same rule, earlier;
      * otherwise the newest row's status (pending, rejected, deleted…),
        so the refusal can say why.
    """
    rows = await template_accessor.templates_by_name(merchant_id, channel, name)
    if not rows:
        return None
    approved_per_account: Dict[str, int] = {}
    for row in rows:
        if row.status == TEMPLATE_APPROVED:
            approved_per_account[row.provider_account_ref] = (
                approved_per_account.get(row.provider_account_ref, 0) + 1
            )
    if approved_per_account:
        crowded = max(approved_per_account.values())
        if crowded > 1:
            return f"approved in {crowded} languages"
        return TEMPLATE_APPROVED
    return rows[0].status


async def approved_template(
    merchant_id: str, channel: str, provider_account_ref: str, name: str
) -> Optional[ApprovedTemplate]:
    """Is this template name approved on this account — and if so, the row.

    The registry's one public read for the send path. It states a FACT and
    nothing else — the caller owns the word for "no", exactly as the binding
    and installation steps already separate the fact from the refusal. It also
    keeps every read of the registry table in this file: two logic files
    owning reads on one table is how two answers to "is this approved" appear.

    The answer is the ROW, not one of its fields: which field a send needs is
    the adapter's business (WhatsApp renders by language, SMS-DLT sends the
    provider's id), and a registry that answered "the language" would be
    answering WhatsApp's question for every channel.

    None has three causes, and from the sender's side they are one fact:

      * the name was never registered here;
      * it is registered but pending / rejected / paused / deleted;
      * it is approved in MORE THAN ONE language.

    The last deserves the same None as the others rather than a default:
    crm_message carries the template NAME and no language column, so picking a
    locale would be guessing which language a customer reads, and a wrong
    guess is an unreadable message sent under a merchant's name. When T16
    grows a language column this becomes a lookup on the full natural key and
    the ambiguity disappears — noted as the trail, not worked around here.
    """
    approved = await template_accessor.approved_templates_for_send(
        merchant_id, channel, provider_account_ref, name
    )
    if len(approved) != 1:
        logger.warning(
            f"connectivity: template '{name}' on {provider_account_ref} has "
            f"{len(approved)} approved rows for {merchant_id}/{channel} — "
            f"exactly one is required to send"
        )
        return None
    return approved[0]
