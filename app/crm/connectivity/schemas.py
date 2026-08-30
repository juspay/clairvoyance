"""Leaf shapes for the connectivity module. Imports nothing internal."""

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


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
