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

from typing import List, Optional

from app.core.logger import logger
from app.crm.connectivity.db.accessors import (
    binding as binding_accessor,
    installation as installation_accessor,
    template as template_accessor,
)
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
    one publish-time read (rollout phase 08, G12), asked of the ACCOUNT the
    route will send from.

    A workflow's send node names a template and a channel, never a provider
    account; the route picks the account at send time — and it always picks
    the same one: the merchant's primary active pipe on the channel
    (partial-unique, primary_binding_query) and that pipe's installation.
    So this is the send door's question asked early, of that one account.
    Not "approved somewhere" (an approval on a second account the route
    never uses passed publish and then blocked every send — the #1080
    review), and not "clean on every account" (a pending copy on a second
    account would refuse a board that only ever sends from the first):

      * no active primary pipe on the channel — nothing can send; say so;
      * no row under that name on the sending account — never registered
        there (a row on another account does not count);
      * more than one approved row there — the language ambiguity
        approved_template refuses at send time, refused here first;
      * no approved row — the newest row's status, so the refusal says why;
      * exactly one approved row — publishable.

    Verdict-shaped: outreach reads ``publishable`` and quotes ``reason``
    (the clause after the template's name); it never compares a status word
    across the seam. A send node that names its own pipe
    (Message.binding_id — nothing sets it today) is the trigger for this
    read to take that binding instead of the primary: the same lookup, one
    parameter earlier.
    """
    binding = await binding_accessor.get_binding(merchant_id, channel, None)
    installation = (
        await installation_accessor.get_installation(
            merchant_id, binding.installation_id
        )
        if binding is not None
        else None
    )
    if installation is None:
        return TemplateVerdict(
            publishable=False,
            reason=f"cannot be sent: no active primary {channel} pipe is connected",
        )
    account = installation.external_account_id
    rows = [
        row
        for row in await template_accessor.templates_by_name(merchant_id, channel, name)
        if row.provider_account_ref == account
    ]
    if not rows:
        return TemplateVerdict(
            publishable=False,
            reason=f"is not registered on the sending {channel} account ({account})",
        )
    approved = [row for row in rows if row.status == TEMPLATE_APPROVED]
    if len(approved) > 1:
        return TemplateVerdict(
            publishable=False,
            reason=f"is approved in {len(approved)} languages on the sending "
            f"{channel} account — exactly one is required to send",
        )
    if not approved:
        return TemplateVerdict(
            publishable=False,
            reason=f"is '{rows[0].status}', not approved, on the sending "
            f"{channel} account",
        )
    return TemplateVerdict(publishable=True)


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
