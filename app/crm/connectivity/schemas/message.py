"""Outbound-message shapes: one claimed attempt, the grant that authorises
it, everything the sender needs resolved, and what came back — plus the
free-form reply bodies a session send carries."""

from datetime import datetime
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from app.crm.connectivity.schemas.connector import ChannelBinding, ConnectorInstallation
from app.crm.connectivity.schemas.template import ApprovedTemplate


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


class SendBehind(BaseModel):
    """Who caused the message a provider's id names — the reply join.

    A customer's reply carries the provider's id for the message she
    answered (Meta's wamid in ``context.id``) and nothing of ours, so this
    is how a producer learns the answer is to ITS send: canon T16 col 7/8
    record what caused a send, and col 14's partial UNIQUE on
    provider_message_id is the index the canon describes as "how an inbound
    receipt finds this row". A reply is a receipt of another kind.

    ``dedupe_key`` rides along because it is the producer's OWN name for the
    send — for a workflow, ``<run>:<node>``, which names the square as well
    as the run. Narrow on purpose: a caller learning who to wake has no
    business with the manifest's status, address or variables.
    """

    source_kind: str
    source_id: Optional[str] = None
    dedupe_key: str


class SendOutcome(BaseModel):
    """What a connector reports back: what the provider DID, never what the
    row should become — that decision stays in dispatch.py. ``reason`` is
    shown to merchants, so "error" is not a reason.

    'blocked' is OUR refusal (gate, no route); 'failed' is the provider's
    (T16 col 12) — a row is refused by us or by them, never both.
    """

    status: Literal["accepted", "failed", "blocked"]
    provider_message_id: Optional[str] = None
    # Which pipe it LEFT on (T16 col 6; set-once on the row, migration 060):
    # stamped by send() on an accepted outcome, None otherwise — a blocked or
    # failed message never left, and a retry may leave on another pipe.
    binding_id: Optional[str] = None
    reason: Optional[str] = None
    retryable: bool = False


class SendToken(BaseModel):
    """The gate's grant for ONE message. Presented to send(), consumed there.

    dispatch.py mints one only after gate() allows the message — today the
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
    #
    # A real import, not a forward reference: ApprovedTemplate lived further
    # down the same file before the split, so it needed quoting and a
    # SendRoute.model_rebuild() at the bottom. Importing the family it
    # belongs to resolves the annotation eagerly and both go away.
    template: Optional[ApprovedTemplate] = None


# ---------------------------------------------------------------------------
# Session sends — free-form replies inside the customer-service window
# ---------------------------------------------------------------------------
#
# Channel-NEUTRAL on purpose: these say what the reply IS (text, a few reply
# buttons, a list, an image), never how a provider spells it. Structure is
# checked here (non-empty, unique ids, https); the channel's LIMITS (three
# buttons, 20-character titles) are the channel's facts and are checked
# against its ConversationProfile at the send door, so one model serves
# every channel with a conversation.


class _SessionBodyBase(BaseModel):
    #: The provider's id for the message this reply quotes (Meta's wamid),
    #: or None. Quoting is presentation only: it never changes who the reply
    #: goes to.
    reply_to: Optional[str] = Field(None, min_length=1, max_length=512)


class TextBody(_SessionBodyBase):
    kind: Literal["text"] = "text"
    text: str = Field(..., min_length=1)
    #: Let the channel render a link preview for the first URL in the text.
    preview_url: bool = False


class ReplyButton(BaseModel):
    """One tappable reply. ``id`` comes back on the customer's tap."""

    id: str = Field(..., min_length=1, max_length=256)
    title: str = Field(..., min_length=1)


class ButtonsBody(_SessionBodyBase):
    kind: Literal["buttons"] = "buttons"
    text: str = Field(..., min_length=1)
    buttons: List[ReplyButton] = Field(..., min_length=1)
    header: Optional[str] = Field(None, min_length=1)
    footer: Optional[str] = Field(None, min_length=1)

    @field_validator("buttons")
    @classmethod
    def _ids_are_unique(cls, buttons: List[ReplyButton]) -> List[ReplyButton]:
        """Two buttons with one id make the tap ambiguous — which one did
        she mean? — so the reply is refused rather than guessed."""
        ids = [button.id for button in buttons]
        if len(ids) != len(set(ids)):
            raise ValueError("button ids must be unique")
        return buttons


class ListRow(BaseModel):
    """One pickable row. ``id`` comes back on the customer's choice."""

    id: str = Field(..., min_length=1, max_length=200)
    title: str = Field(..., min_length=1)
    description: Optional[str] = Field(None, min_length=1)


class ListBody(_SessionBodyBase):
    kind: Literal["list"] = "list"
    text: str = Field(..., min_length=1)
    #: The label on the control that opens the list.
    button: str = Field(..., min_length=1)
    rows: List[ListRow] = Field(..., min_length=1)
    section_title: Optional[str] = Field(None, min_length=1)
    header: Optional[str] = Field(None, min_length=1)
    footer: Optional[str] = Field(None, min_length=1)

    @field_validator("rows")
    @classmethod
    def _ids_are_unique(cls, rows: List[ListRow]) -> List[ListRow]:
        ids = [row.id for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError("row ids must be unique")
        return rows


class ImageBody(_SessionBodyBase):
    kind: Literal["image"] = "image"
    #: A public https URL the provider fetches. Plain http is refused: the
    #: provider fetches it from the open internet on the customer's behalf.
    url: str = Field(..., min_length=1, max_length=2048)
    caption: Optional[str] = Field(None, min_length=1)

    @model_validator(mode="after")
    def _https_only(self) -> "ImageBody":
        if not self.url.lower().startswith("https://"):
            raise ValueError("image url must be https")
        return self


#: Every free-form reply shape — the one spelling of the union. Adding a
#: body kind is this line plus its model; everything else imports it.
SessionBodyType = Union[TextBody, ButtonsBody, ListBody, ImageBody]

#: The same union, as a request body parses it (by ``kind``).
SessionBody = Annotated[SessionBodyType, Field(discriminator="kind")]


def body_text(body: SessionBodyType) -> Optional[str]:
    """PURE: the words a person reads in ``body`` — the text, or an image's
    caption. What the message.queued letter's readers show as a preview."""
    if isinstance(body, ImageBody):
        return body.caption
    return body.text


class SessionSendResult(BaseModel):
    """What one session send came to.

    ``status`` is the manifest row's word: accepted (the provider took it),
    failed (the provider refused — ``retryable`` says whether the same words
    could plausibly go through later), or blocked (WE refused: the gate, no
    route, a body that does not fit the channel). A session send is never
    queued and never retried by the dispatcher — its words are not on the
    row — so a retry is the caller's, under a NEW dedupe key.

    ``duplicate`` means the dedupe key already named a row: nothing was sent
    this time, and the fields describe that earlier row as it stands.
    """

    message_id: str
    status: str
    reason: Optional[str] = None
    provider_message_id: Optional[str] = None
    retryable: bool = False
    duplicate: bool = False


class MessageState(BaseModel):
    """One manifest row's current word — what a repeated session send (same
    dedupe key) reports instead of sending again."""

    id: str
    status: str
    reason: Optional[str] = None
    provider_message_id: Optional[str] = None


class ProviderReceipt(BaseModel):
    """What a delivery receipt says about one of our messages, read through
    the event catalog's declared fields — never a provider's payload shape.

    ``state`` is sent · delivered · read · failed; anything else is not a
    receipt this module acts on.
    """

    provider_message_id: str
    state: str
    occurred_at: Optional[datetime] = None
    #: The provider's refusal code on a failed receipt (canon T16 col 13:
    #: the row keeps the provider's own word).
    error_code: Optional[str] = None
    #: What the provider billed the message as (Meta's pricing.category),
    #: when it says.
    pricing_category: Optional[str] = None
