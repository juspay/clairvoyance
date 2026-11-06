"""Outbound-message shapes every channel shares: one claimed attempt, the
grant that authorises it, everything the sender needs resolved, what came
back, and what a delivery receipt says about it."""

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel

from app.crm.connectivity.schemas.connector import (
    ChannelBinding,
    ConnectorInstallation,
    CredentialBundle,
)
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

    gate.py mints one only after gate() allows the message — today the
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


class SendRoute(BaseModel):
    """Everything a sender needs, resolved in one call — so no adapter ever
    asks the database anything, which is what keeps them testable without
    one."""

    installation: ConnectorInstallation
    binding: ChannelBinding
    bundle: CredentialBundle
    # The approved registry row (T23) this send renders — or None on a
    # channel that does not pre-register templates (channels/registry.py
    # decides which do), and for a free-form reply, which names no template.
    # Channel-neutral on purpose: the WhatsApp adapter reads its
    # language, an SMS-DLT adapter will read its provider_template_id, an
    # email adapter reads nothing. The route carries the ROW and each adapter
    # takes the field it needs; a field named for one provider's need would
    # be the first thing the second adapter has to work around. For a
    # template send on a channel that registers, resolve_send_route refuses
    # before the adapter rather than passing None.
    #
    # A real import, not a forward reference: ApprovedTemplate lived further
    # down the same file before the split, so it needed quoting and a
    # SendRoute.model_rebuild() at the bottom. Importing the family it
    # belongs to resolves the annotation eagerly and both go away.
    template: Optional[ApprovedTemplate] = None


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


class ReplySendResult(BaseModel):
    """What one reply send came to.

    ``status`` is the manifest row's word: accepted (the provider took it),
    failed (the provider refused, or no answer came: the gate's probe timed
    out, or the send errored or timed out — ``retryable`` says whether the
    same words could plausibly go through later), or blocked (WE refused:
    the gate, no route, a body that does not fit the channel). A reply send
    is never queued and never retried by the dispatcher — its words
    are not on the row — so a retry is the caller's, under a NEW dedupe key.

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
    """One manifest row's current word — what a repeated reply send (same
    dedupe key) reports instead of sending again."""

    id: str
    status: str
    reason: Optional[str] = None
    provider_message_id: Optional[str] = None


class MessageTick(BaseModel):
    """What became of one manifest row, as a timeline shows it: its status
    word, the reason when it failed, and when each tick landed."""

    id: str
    status: str
    reason: Optional[str] = None
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    read_at: Optional[datetime] = None
