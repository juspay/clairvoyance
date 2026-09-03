"""The template registry's READS (T23) — every read of the table, in one file.

templates.py owns the four lifecycle transitions (create · submit · edit ·
retire); this file owns what the rest of the system ASKS the registry:

  get / list_templates   the console's reads
  template_status        the publish-time question (rollout phase 08, G12),
                         verdict-shaped so the words never leave this module
  approved_template      the send-time question — the ROW, not a field

Two logic files owning reads on one table is how two answers to "is this
approved" appear; that is why the send door and the publish check both ask
here, and why the transitions file reads nothing itself.
"""

from typing import Dict, List, Optional

from app.core.logger import logger
from app.crm.connectivity.db.accessors import template as template_accessor
from app.crm.connectivity.schemas.template import (
    ApprovedTemplate,
    TemplateRead,
    TemplateVerdict,
)
from app.crm.connectivity.status import TEMPLATE_APPROVED


async def get(merchant_id: str, template_id: str) -> Optional[TemplateRead]:
    return await template_accessor.get_template(merchant_id, template_id)


async def list_templates(
    merchant_id: str, channel: Optional[str] = None, status: Optional[str] = None
) -> List[TemplateRead]:
    return await template_accessor.list_templates(merchant_id, channel, status)


async def template_status(merchant_id: str, channel: str, name: str) -> TemplateVerdict:
    """Is this template NAME publishable on this channel — the registry's
    one publish-time read (rollout phase 08, G12).

    A workflow's send node names a template and a channel, never the
    provider account that will serve it (the route picks that at send
    time), so the question is asked across the merchant's accounts:

      * no row under that name — never registered here;
      * every account holding the name holds exactly ONE approved row —
        publishable (the send door will find its one row whichever account
        the route picks);
      * some account holds several approved rows — the ambiguity
        approved_template refuses at send time, refused here first;
      * otherwise the newest row's status, so the refusal can say why.

    Verdict-shaped (3 Sep 2026 audit): outreach reads ``publishable`` and
    quotes ``reason``; it never compares a status word of this module's
    across the seam. ``reason`` is the clause after the template's name.
    """
    rows = await template_accessor.templates_by_name(merchant_id, channel, name)
    if not rows:
        return TemplateVerdict(
            publishable=False,
            reason=f"is not registered on {channel} for this merchant",
        )
    approved_per_account: Dict[str, int] = {}
    for row in rows:
        if row.status == TEMPLATE_APPROVED:
            approved_per_account[row.provider_account_ref] = (
                approved_per_account.get(row.provider_account_ref, 0) + 1
            )
    if approved_per_account:
        crowded = max(approved_per_account.values())
        if crowded > 1:
            return TemplateVerdict(
                publishable=False,
                reason=f"is approved in {crowded} languages on one account — "
                "exactly one is required to send",
            )
        return TemplateVerdict(publishable=True)
    return TemplateVerdict(
        publishable=False, reason=f"is '{rows[0].status}', not approved"
    )


async def approved_template(
    merchant_id: str, channel: str, provider_account_ref: str, name: str
) -> Optional[ApprovedTemplate]:
    """Is this template name approved on this account — and if so, the row.

    The registry's one public read for the send path. It states a FACT and
    nothing else — the caller owns the word for "no", exactly as the binding
    and installation steps already separate the fact from the refusal.

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
