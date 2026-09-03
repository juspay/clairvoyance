"""Template-registry shapes: what crosses the TemplateProvider port, and the
request/read models the console speaks."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.crm.connectivity.schemas.tenancy import TenantScoped


class TemplateDraft(BaseModel):
    """What a provider needs to register a template. The local row's id and
    status are deliberately absent: the provider has no business knowing
    them, and passing them invites a face to make a lifecycle decision."""

    name: str
    language: str
    category: str
    components: List[Dict[str, Any]]


class ApprovedTemplate(BaseModel):
    """The send path's answer from the registry: this name is approved, and
    these are the facts about it an adapter may need to send.

    Narrow on purpose — resolve_send_route runs once per message, and the row
    carries a components blob the send path never renders (the provider does)
    — but not narrower than the CHANNELS it serves: language is what WhatsApp
    renders by, provider_template_id is what an SMS-DLT header carries, and
    category is what the gate will map a purpose against. One shape, every
    adapter reads its own field.
    """

    id: str
    name: str
    language: str
    provider_template_id: Optional[str] = None
    category: Optional[str] = None


class ProviderTemplateState(BaseModel):
    """A provider's answer about one template, in the CANON's vocabulary.

    Normalisation happens in the provider face, never here and never in
    templates.py: Meta shouting APPROVED while canon T23 spells it
    'approved' is Meta's quirk, and a registry that stored both would have
    every status filter silently return the wrong half of its rows. That is
    not hypothetical — it shipped once and was found by running it.
    """

    provider_template_id: Optional[str] = None
    name: Optional[str] = None
    language: Optional[str] = None
    status: Optional[str] = None
    #: The category the provider ASSIGNED, which may differ from the one we
    #: requested — the difference is a price change, so it is kept visible.
    category: Optional[str] = None
    quality: Optional[str] = None
    rejection_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# The API surface — request bodies and read models
# ---------------------------------------------------------------------------


class CreateTemplateDraftRequest(TenantScoped):
    """Body for POST /connectors/templates. ``components`` is stored verbatim — the
    provider's own registered structure — and is never validated against
    their schema here: that is the provider's job at submission time, and a
    second validator would refuse shapes they accept."""

    channel: str = Field(..., description="Must name a registered connector")
    provider_account_ref: str = Field(
        ..., description="The account that owns this template (a WABA id)"
    )
    name: str = Field(..., description="What crm_message.template_id will store")
    language: str = Field(
        ..., description="The provider's key is (account, name, language)"
    )
    components: List[Dict[str, Any]] = Field(
        ..., description="The registered structure, verbatim"
    )


class SubmitTemplateRequest(TenantScoped):
    category: str = Field(
        ...,
        description=(
            "The provider's own category vocabulary, stored as theirs "
            "(Meta: MARKETING · UTILITY · AUTHENTICATION)"
        ),
    )


class EditTemplateRequest(TenantScoped):
    components: List[Dict[str, Any]] = Field(
        ..., description="Replacement components, verbatim"
    )


class RetireTemplateRequest(TenantScoped):
    """Body for POST /connectors/templates/{id}/retire — the tenant, nothing
    else; the id rides in the path."""


class TemplateVerdict(BaseModel):
    """The publish-time answer for one template NAME on one channel (rollout
    phase 08, G12), verdict-shaped so the words stay inside connectivity:
    outreach reads ``publishable`` and quotes ``reason`` — it never compares a
    status word across the module seam. ``reason`` is the clause after the
    template's name in a refusal ("is 'pending', not approved"); None when
    publishable."""

    publishable: bool
    reason: Optional[str] = None


class TemplateRead(BaseModel):
    """One registry row (T23). ``category`` is what the provider ASSIGNED and
    ``submitted_category`` what we asked for — the difference is a price
    change, so both stay visible rather than one overwriting the other."""

    id: str
    merchant_id: str
    channel: str
    provider_account_ref: str
    name: str
    language: str
    provider_template_id: Optional[str] = None
    category: Optional[str] = None
    submitted_category: Optional[str] = None
    category_updated_at: Optional[datetime] = None
    components: List[Dict[str, Any]] = Field(default_factory=list)
    status: str
    status_updated_at: datetime
    rejection_reason: Optional[str] = None
    quality: str
    quality_updated_at: Optional[datetime] = None
    last_synced_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
