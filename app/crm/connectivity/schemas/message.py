"""Outbound-message shapes: one claimed attempt, the grant that authorises
it, everything the sender needs resolved, and what came back."""

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

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
