"""Template registry (crm_template, T23) — owned by connectivity.

BUSINESS LOGIC ONLY — DB mechanics live in db/accessor.py, Meta Graph API
calls in meta_graph.py. Status vocabulary and the one hard rule
canon states explicitly — editing an approved template resets it to
pending, Meta re-reviews in place — live here, not as a DB trigger,
because it's a rule about a *transition*, not an invariant on a row.

Freshness for now is the periodic full-sync only (last_synced_at, "the
drift healer") — the webhook-driven consumer canon also describes is
deferred: no WhatsApp inbound webhook receiver exists in this repo yet,
and building one is a separate record-module change.
"""

import json
from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.core.logger.context import set_log_context
from app.crm.connectivity import meta_graph as whatsapp
from app.crm.connectivity.db import DbTxn, accessor, atomically
from app.crm.connectivity.schemas import TemplateRead
from app.database.accessor.breeze_buddy.credentials import get_credential_by_id

CONNECTOR_KEY = "whatsapp"

_EDITABLE_STATUSES = {"draft"}
_SUBMITTABLE_STATUSES = {"draft", "rejected"}
# Exclusive to draft/rejected — two concurrent submit() calls must never
# both pass this claim. A crash after Meta accepted but before the local
# write lands is resumed by sync_installation_templates() matching the
# natural key against a 'submitting' row with provider_template_id IS NULL,
# not by re-claiming here (that path would re-POST and Meta would reject
# the duplicate name).
_CLAIMABLE_FOR_SUBMIT = ["draft", "rejected"]


def _from_meta(status: Optional[str]) -> Optional[str]:
    """Meta's Graph API returns statuses in UPPERCASE; canon (T23) names
    them lowercase. Unknown words pass through lowercased (canon: '+
    whatever Meta adds') rather than being rejected."""
    return status.lower() if status is not None else None


class TemplateError(Exception):
    """A requested transition isn't supported from the template's current
    status, or the installation/credential backing it can't be resolved."""


class TemplateNotFoundError(TemplateError):
    """template_id doesn't resolve for this merchant — a 404, not a 400."""


async def create_draft(
    merchant_id: str,
    channel: str,
    provider_account_ref: str,
    name: str,
    language: str,
    components: List[Dict[str, Any]],
) -> TemplateRead:
    """Idempotent on the natural key (merchant_id, channel,
    provider_account_ref, name, language) — but only while the existing row
    is still a draft (law #4's idempotency does not extend to silently
    overwriting a template that's already been submitted)."""
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
    """ATOMIC: the natural-key check and the write share one fate — a
    concurrent second draft call for the same key must not race past the
    check and duplicate the unique index's job with a confusing error."""
    components_json = json.dumps(components)
    existing = await accessor.get_template_by_natural_key(
        txn, merchant_id, channel, provider_account_ref, name, language
    )
    if existing is None:
        return await accessor.insert_template_draft(
            txn,
            merchant_id,
            channel,
            provider_account_ref,
            name,
            language,
            components_json,
        )
    if existing.status != "draft":
        raise TemplateError(
            f"template {existing.id} is '{existing.status}', not draft — "
            "cannot overwrite by recreating"
        )
    updated = await accessor.update_draft_components(
        txn, merchant_id, existing.id, provider_account_ref, components_json
    )
    if updated is None:
        raise TemplateError(f"template {existing.id} draft update raced")
    return updated


async def _resolve_access_token(merchant_id: str, provider_account_ref: str) -> str:
    installation = await accessor.get_installation_credential(
        merchant_id, CONNECTOR_KEY, provider_account_ref
    )
    if installation is None or installation["status"] != "healthy":
        raise TemplateError(
            f"no healthy whatsapp installation for provider_account_ref "
            f"{provider_account_ref}"
        )
    credential = await get_credential_by_id(installation["credential_id"], mask=False)
    if (
        credential is None
        or not credential.value
        or whatsapp.TOKEN_KEY not in credential.value
    ):
        raise TemplateError(
            f"credential for installation {installation['id']} is missing or unreadable"
        )
    return credential.value[whatsapp.TOKEN_KEY]


async def submit(merchant_id: str, template_id: str, category: str) -> TemplateRead:
    template = await accessor.get_template(merchant_id, template_id)
    if template is None:
        raise TemplateNotFoundError(f"template {template_id} not found")
    if template.status not in _SUBMITTABLE_STATUSES:
        raise TemplateError(
            f"template {template_id} is '{template.status}' — submit only "
            "valid from draft or rejected"
        )

    claimed = await atomically(_claim_for_submit_in_txn, merchant_id, template_id)
    if claimed is None:
        raise TemplateError(
            f"template {template_id} is already being submitted — concurrent "
            "submit rejected"
        )

    access_token = await _resolve_access_token(
        merchant_id, claimed.provider_account_ref
    )
    response = await whatsapp.create_message_template(
        claimed.provider_account_ref,
        access_token,
        claimed.name,
        claimed.language,
        category,
        claimed.components,
    )
    updated = await atomically(
        _submit_in_txn,
        merchant_id,
        template_id,
        response["id"],
        response.get("category", category),
        category,
        _from_meta(response.get("status", "pending")) or "pending",
    )
    return updated


async def _claim_for_submit_in_txn(
    txn: DbTxn, merchant_id: str, template_id: str
) -> Optional[TemplateRead]:
    """ATOMIC: the status-vs-'submitting' compare and the swap share one
    fate — two concurrent submit() calls must not both see themselves as
    the one holding the claim."""
    return await accessor.claim_template_for_submit(
        txn, merchant_id, template_id, _CLAIMABLE_FOR_SUBMIT
    )


async def _submit_in_txn(
    txn: DbTxn,
    merchant_id: str,
    template_id: str,
    provider_template_id: str,
    category: str,
    submitted_category: str,
    status: str,
) -> TemplateRead:
    """ATOMIC: one row's provider id + category + status land together —
    a caller must never see a template with Meta's id but our stale status."""
    updated = await accessor.submit_template(
        txn,
        merchant_id,
        template_id,
        provider_template_id,
        category,
        submitted_category,
        status,
    )
    if updated is None:
        raise TemplateNotFoundError(f"template {template_id} not found on submit")
    return updated


async def edit(
    merchant_id: str, template_id: str, components: List[Dict[str, Any]]
) -> TemplateRead:
    template = await accessor.get_template(merchant_id, template_id)
    if template is None:
        raise TemplateNotFoundError(f"template {template_id} not found")

    if template.status in _EDITABLE_STATUSES:
        components_json = json.dumps(components)
        updated = await atomically(
            _edit_draft_in_txn, merchant_id, template_id, components_json
        )
        return updated

    if template.status == "approved":
        if template.provider_template_id is None:
            raise TemplateError(f"template {template_id} has no provider_template_id")
        access_token = await _resolve_access_token(
            merchant_id, template.provider_account_ref
        )
        await whatsapp.edit_message_template(
            template.provider_template_id, access_token, components
        )
        components_json = json.dumps(components)
        updated = await atomically(
            _edit_approved_in_txn, merchant_id, template_id, components_json
        )
        return updated

    raise TemplateError(
        f"template {template_id} is '{template.status}' — edit not supported "
        "from this status"
    )


async def _edit_draft_in_txn(
    txn: DbTxn, merchant_id: str, template_id: str, components_json: str
) -> TemplateRead:
    """ATOMIC: a draft edit is one statement — no external call precedes
    it, so this exists only to route through the one boundary door."""
    updated = await accessor.update_draft_components_only(
        txn, merchant_id, template_id, components_json
    )
    if updated is None:
        raise TemplateError(f"template {template_id} draft edit raced")
    return updated


async def _edit_approved_in_txn(
    txn: DbTxn, merchant_id: str, template_id: str, components_json: str
) -> TemplateRead:
    """ATOMIC: the components write and the status-reset-to-pending share
    one fate (canon: editing an approved template puts the SAME row back
    to pending) — a reader must never see new components with the old
    'approved' status still attached."""
    updated = await accessor.update_approved_components(
        txn, merchant_id, template_id, components_json
    )
    if updated is None:
        raise TemplateError(f"template {template_id} approved-edit raced")
    return updated


async def retire(merchant_id: str, template_id: str) -> TemplateRead:
    template = await accessor.get_template(merchant_id, template_id)
    if template is None:
        raise TemplateNotFoundError(f"template {template_id} not found")
    if template.provider_template_id is not None:
        try:
            access_token = await _resolve_access_token(
                merchant_id, template.provider_account_ref
            )
            await whatsapp.delete_message_template(
                template.provider_account_ref, access_token, template.name
            )
        except whatsapp.WhatsappProviderError as e:
            logger.warning(f"template {template_id} delete on Meta failed: {e}")
    updated = await accessor.retire_template(merchant_id, template_id)
    if updated is None:
        raise TemplateNotFoundError(f"template {template_id} not found on retire")
    return updated


async def get(merchant_id: str, template_id: str) -> Optional[TemplateRead]:
    return await accessor.get_template(merchant_id, template_id)


async def list_templates(
    merchant_id: str, channel: Optional[str] = None, status: Optional[str] = None
) -> List[TemplateRead]:
    return await accessor.list_templates(merchant_id, channel, status)


async def sync_installation_templates(installation: Dict[str, Any]) -> None:
    """The periodic drift healer for one installation — pulls Meta's full
    template list and updates local status/category/quality/rejection
    fields by matching on provider_template_id. A Meta template with no
    matching local row (submitted outside this system) is logged and
    skipped — this pass never backfills drafts we never created."""
    credential = await get_credential_by_id(installation["credential_id"], mask=False)
    if (
        credential is None
        or not credential.value
        or whatsapp.TOKEN_KEY not in credential.value
    ):
        logger.warning(
            f"sync skipped: installation {installation['id']} credential unreadable"
        )
        return
    access_token = credential.value[whatsapp.TOKEN_KEY]
    waba_id = installation["external_account_id"]
    try:
        remote_templates = await whatsapp.list_message_templates(waba_id, access_token)
    except whatsapp.WhatsappProviderError as e:
        logger.warning(f"sync failed for installation {installation['id']}: {e}")
        return

    for remote in remote_templates:
        provider_template_id = remote.get("id")
        if not provider_template_id:
            continue
        status = _from_meta(remote.get("status"))
        category = remote.get("category")
        quality = (
            remote.get("quality_score", {}).get("score")
            if remote.get("quality_score")
            else None
        )
        rejection_reason = remote.get("rejected_reason")
        updated = await accessor.sync_template_status(
            provider_template_id,
            category,
            None,
            status,
            quality,
            rejection_reason,
        )
        if updated is None:
            name, language = remote.get("name"), remote.get("language")
            if name and language:
                updated = await accessor.resume_submitted_template(
                    installation["merchant_id"],
                    waba_id,
                    name,
                    language,
                    provider_template_id,
                    category,
                    status,
                    quality,
                    rejection_reason,
                )
        if updated is None:
            logger.info(
                f"sync: no local template for provider_template_id "
                f"{provider_template_id} (installation {installation['id']}) — skipped"
            )


async def sync_all_installations() -> None:
    """Entry point for the background scheduler — walks every healthy
    WhatsApp installation across every merchant. One installation's
    failure (expired token, Meta outage) must not block the rest."""
    installations = await accessor.list_active_installations_for_sync(CONNECTOR_KEY)
    for installation in installations:
        set_log_context(
            component="crm.connectivity.template_sync",
            merchant_id=installation["merchant_id"],
            installation_id=installation["id"],
        )
        try:
            await sync_installation_templates(installation)
        except Exception as e:
            logger.error(
                f"template sync failed for installation {installation['id']}: {e}"
            )
