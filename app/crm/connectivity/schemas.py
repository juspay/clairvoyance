"""Leaf shapes for the connectivity module (module rules §1). Imports
nothing internal — api.py, contracts.py, the provider ports and the tests
all read their vocabulary from here."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class QueuedMessage(BaseModel):
    """One claimed outbound attempt, as the dispatcher works on it."""

    id: str
    merchant_id: str
    customer_id: str
    channel: str
    sent_to_address: str
    binding_id: Optional[str] = None
    source_kind: str
    source_id: Optional[str] = None
    # Unused until the permission check lands: it grants per purpose, not per
    # customer, so the gate cannot answer without this.
    purpose_key: str
    template_id: Optional[str] = None
    variables: Dict[str, Any] = {}
    dedupe_key: str
    attempt: int = 0
    # When the row became eligible (timestamptz NOT NULL). Carried for the
    # queue-lag metric, not for any dispatch decision.
    next_attempt_at: datetime


class SendOutcome(BaseModel):
    """What a connector reports back: what the provider DID, never what the
    row should become — that decision stays in dispatch.py. ``reason`` is
    shown to merchants, so "error" is not a reason.

    'blocked' is OUR refusal (gate, no route); 'failed' is the provider's
    (T16 col 12) — a row is refused by us or by them, never both.
    """

    status: Literal["accepted", "failed", "blocked"]
    provider_message_id: Optional[str] = None
    reason: Optional[str] = None
    retryable: bool = False


class SendToken(BaseModel):
    """The gate's grant for ONE message. Presented to send(), consumed there.

    dispatch.py mints one only after _gate() allows the message — today the
    suppression slice (fail closed), until the full may_contact() (consent,
    purpose, quiet hours — the permission module's B5) replaces the gate's
    body. send() refuses a token that does not name this exact message, so
    one grant can never authorise a batch.
    """

    message_id: str
    purpose_key: str
    granted: bool = False
    # Points at the permission decision that authorised the send; stamped onto
    # the manifest row once the diary exists.
    decision_id: Optional[int] = None


class ConnectorInstallation(BaseModel):
    """A merchant's account on one connector — the door.

    Holds no secret: ``credential_id`` says where the bundle lives.
    """

    id: str
    merchant_id: str
    connector_key: str
    external_account_id: str
    display_label: Optional[str] = None
    credential_id: Optional[str] = None
    status: str
    token_expires_at: Optional[datetime] = None


class ChannelBinding(BaseModel):
    """One real endpoint under an installation — the pipe.

    ``address`` is the provider's identifier for it (a Meta phone_number_id,
    a sender id, a from-address); what it means is the channel's business.
    """

    id: str
    merchant_id: str
    channel: str
    installation_id: str
    address: str
    capabilities: Dict[str, Any] = {}
    is_primary: bool = False
    status: str


class CredentialBundle(BaseModel):
    """One installation's whole key bundle, decrypted.

    A bag, not a schema: what keys a connector needs is the adapter's
    business. ``repr=False`` means an accidental f-string prints
    CredentialBundle(), not a live token — the cheapest guard against
    leaking a secret into a log aggregator.
    """

    values: Dict[str, Any] = Field(default_factory=dict, repr=False)

    def secret(self, key: str) -> Optional[str]:
        """The named secret, or None. Callers fail closed on None; a bundle
        missing the key it needs is a broken connection, not a retry."""
        value = self.values.get(key)
        return value if isinstance(value, str) and value else None


class SendRoute(BaseModel):
    """Everything a sender needs, resolved in one call — so no adapter ever
    asks the database anything, which is what keeps them testable without
    one."""

    installation: ConnectorInstallation
    binding: ChannelBinding
    bundle: CredentialBundle
    # The approved registry row (T23) this send renders — or None on a
    # channel that does not pre-register templates (channels.py decides
    # which do). Channel-neutral on purpose: the WhatsApp adapter reads its
    # language, an SMS-DLT adapter will read its provider_template_id, an
    # email adapter reads nothing. The route carries the ROW and each adapter
    # takes the field it needs; a field named for one provider's need would
    # be the first thing the second adapter has to work around. For a channel
    # that registers, resolve_send_route refuses before the adapter rather
    # than passing None.
    template: Optional["ApprovedTemplate"] = None


# ---------------------------------------------------------------------------
# Connector onboarding — the shapes crossing the ConnectorOnboarder port
# ---------------------------------------------------------------------------

#: Canon T11's health ladder. A provider face reports how far up it got; only
#: onboarding.py turns that into a row status, so "how healthy" and "what the
#: traffic light says" cannot drift apart per connector.
HealthLevel = Literal["configured", "authenticated", "subscribed", "healthy"]


class OnboardResult(BaseModel):
    """What a provider's handshake produced — facts, no database.

    Everything vendor-shaped has already been spent by the time this crosses
    the port: an Embedded Signup code became a token, a phone_number_id was
    checked against its WABA, a webhook subscription was attempted. What
    comes back is what any connector's onboarding needs.
    """

    #: The provider's own id for the account — a WABA id today.
    external_account_id: str
    #: The endpoint under it that becomes the binding's address (a Meta
    #: phone_number_id). What it means is the channel's business.
    address: str
    #: What the merchant calls this account in the console. Cosmetic, and it
    #: rides the RESULT rather than being read off the request by generic
    #: code, because only the connector knows which of its request fields is
    #: the human-facing name.
    display_label: Optional[str] = None
    #: The whole credential bundle, ready for the vault. Never logged.
    bundle: Dict[str, Any] = Field(default_factory=dict, repr=False)
    #: None means the provider issues a permanent credential — an HONEST
    #: NULL, not a missing value. Canon T11 col 9: the refresh job watches
    #: non-NULL rows, so a wrong NULL is a connector that dies silently.
    token_expires_at: Optional[datetime] = None
    health_level: HealthLevel = "authenticated"
    #: Mandatory below 'healthy' (canon T11): a light that is not green must
    #: carry the sentence explaining it.
    health_why: Optional[str] = None

    @model_validator(mode="after")
    def _why_is_mandatory_below_healthy(self) -> "OnboardResult":
        """canon T11's rule, enforced where it cannot be forgotten.

        status is the traffic light and health_detail is the sentence under
        it. A door that comes back amber with nothing written in `why` gives
        the connections screen a colour and no reason, and whoever looks at
        it next has to reconstruct what failed from logs. Refusing here costs
        a provider face one line; leaving it to a comment costs that person
        an afternoon.
        """
        if self.health_level != "healthy" and not (self.health_why or "").strip():
            raise ValueError(
                f"health_level '{self.health_level}' is below 'healthy' and "
                f"needs a health_why explaining it"
            )
        return self


# ---------------------------------------------------------------------------
# Template registry — the shapes crossing the TemplateProvider port
# ---------------------------------------------------------------------------


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
#
# merchant_id rides in the body (or the query, for GETs) rather than being
# derived from the token: a caller may hold several merchant_ids, so there is
# no single "current" one to infer, and inferring the wrong one is a
# cross-tenant write. api.py validates it against the caller's RBAC scope
# before anything else runs (fail closed on tenancy).


class InstallationRead(BaseModel):
    """One connector account, as the console sees it.

    ``credential_id`` is deliberately absent: a read model that names where a
    secret lives is one screenshot away from being a map to it.
    """

    id: str
    merchant_id: str
    connector_key: str
    external_account_id: str
    display_label: Optional[str] = None
    status: str
    token_expires_at: Optional[datetime] = None
    last_event_at: Optional[datetime] = None
    health_detail: Dict[str, Any] = Field(default_factory=dict)
    installed_at: datetime
    created_at: datetime
    updated_at: datetime


class CreateTemplateDraftRequest(BaseModel):
    """Body for POST /connectors/templates. ``components`` is stored verbatim — the
    provider's own registered structure — and is never validated against
    their schema here: that is the provider's job at submission time, and a
    second validator would refuse shapes they accept."""

    merchant_id: str = Field(..., description="Tenant scope — required")
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


class SubmitTemplateRequest(BaseModel):
    merchant_id: str = Field(..., description="Tenant scope — required")
    category: str = Field(
        ...,
        description=(
            "The provider's own category vocabulary, stored as theirs "
            "(Meta: MARKETING · UTILITY · AUTHENTICATION)"
        ),
    )


class EditTemplateRequest(BaseModel):
    merchant_id: str = Field(..., description="Tenant scope — required")
    components: List[Dict[str, Any]] = Field(
        ..., description="Replacement components, verbatim"
    )


class RetireTemplateRequest(BaseModel):
    merchant_id: str = Field(..., description="Tenant scope — required")


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


# SendRoute names ApprovedTemplate before it is defined; resolve the forward
# reference now rather than on first validation.
SendRoute.model_rebuild()
