"""connectivity — the public surface.

The only file other modules and app/crm/worker_main.py may import.

This module owns everything between "we want to send something" and "the
provider took it": connector accounts, the endpoints under them, the template
registry, the message table, send() and the dispatch pass. It is
channel-agnostic — WhatsApp, Instagram and email are adapters and faces
behind a registry, not packages other modules know about.

What is here, and why each thing is on the surface:

- ``claim_sends`` / ``dispatch_send`` — the dispatcher role's two callables
  for the shared drain-loop scaffold (design/worker-runtime.md). The
  dispatcher sends, and only sends: no other work rides its loop.
- ``queue_message`` — how a producer (the walker's send node first) proposes
  a send: one queued row, no verdict.
- ``send_behind`` — the reply join, in one indexed read: whose send a
  provider's id names (T16 col 7/8 by col 14's partial UNIQUE). A reply
  carries the provider's id for the message it answers, so a producer
  learns the answer is to ITS send without planting a correlate, keeping
  one, or having its authors declare one.
- ``perform_action`` / ``action_names`` / ``ActionError`` — the fourth verb:
  a run asks a connector to DO something (a Shopify tag, an order note).
  ``action_names`` and ``validate_action_args`` are what outreach's
  publish reads, so a plan naming an unknown action or a misspelled
  argument is refused while the author is still editing; ``ActionError``
  is the DEFECT half of the two failures, and the walker parks on it while
  anything else retries. The connector's own ``args_model`` is the contract,
  so no transport, URL or credential is ever authored in a plan.
- ``onboard`` / ``get_installation`` / ``list_installations`` / ``disconnect``
  — connector accounts and the pipes under them. Connector-agnostic:
  ``onboard`` takes a connector_key and a payload, and the CONNECTORS
  registry decides what that payload means.
- the ``*_template`` family — the T23 registry that ``send.py`` resolves
  against before any provider call.
- ``template_status`` / ``registers_templates_for`` — what outreach's
  publish asks so a send node naming an unknown or unapproved template
  is refused at publish, not blocked at dispatch hours later (phase 08).
- ``register_retire_guard`` — the slot worker_main fills with outreach's
  count of open runs naming a template, so retire can refuse to pull a
  template from under a run in flight without this module importing
  outreach (phase 14; the record/consumers.py inversion).
- ``META_INGRESS`` — the Meta bay for record's /ingest/webhooks/{provider}
  door (ingress.py builds it; app/crm/api.py registers it into record's
  INGRESS slot — the same line worker_main writes for consumers, and the
  inversion that keeps rule 12 whole). A generic surface names the vendor
  exactly once, for that one line. Consumers of the filed letters are a
  separate concern.
- ``resubscribe`` — turn a connected account's webhooks (back) on without
  spending a fresh signup code; onboarding subscribes on the happy path,
  this is the recovery door (disconnect's opposite verb, health re-stamped
  by its atom).
- ``reason_label`` — the human word for a message row's stored reason,
  pure. The row keeps the provider's code (canon T16 col 13); any surface
  that SHOWS a row — the coming message read / "why didn't it send" view —
  translates through this at read, so the stored evidence is never
  rewritten.

- ``consume_template_event`` — the spine consumer that turns a provider's
  template webhook into a registry row change (approved, rejected, paused,
  deleted, a re-categorisation, a quality read). worker_main registers it
  through record's consumer slot, the same inversion the retire guard and
  the ingress bay use. It is the ONLY writer of provider-decided template
  state, and there is deliberately no timer beside it: the periodic sync
  was removed before it ever ran.

- ``send_session`` and ``TextBody`` — a free-form reply inside the
  customer-service window, sent NOW (the conversations module and Buddy's
  turns are its callers). Same manifest, same gate, same send
  door as a template; the words ride the message.queued letter (D1).
  ``conversation_profile`` is the channel's limits, so a caller shapes a
  reply to fit instead of having it refused; ``conversation_channels`` is
  every channel that carries a conversation at all, so a caller iterates
  them instead of naming one.
- ``consume_status_event`` — the receipts consumer (worker_main registers
  it): message.status letters move the manifest along sent -> delivered ->
  read, or to failed with the provider's code.
- ``buddy_binding`` / ``conversation_settings`` / ``list_channel_settings``
  / ``update_channel_settings`` — the merchant's bindings: the one templates
  go out from, and the one Buddy answers on with Buddy's settings (R1,
  D13–D15, D24–D28); read total and fail-closed (no agent, handoff off).

``send()`` stays OFF this surface so that nothing outside the module can
reach a provider without passing the checks in front of it. So does the
route resolver, and so do the provider packages.
"""

from app.crm.connectivity.actions import (
    action_names,
    perform_action,
    validate_action_args,
)
from app.crm.connectivity.channels import (
    conversation_channels,
    conversation_profile,
    registers_templates_for,
)
from app.crm.connectivity.connectors import ActionError
from app.crm.connectivity.dispatch import claim_sends, dispatch_send
from app.crm.connectivity.ingress import META_INGRESS
from app.crm.connectivity.onboarding import (
    disconnect,
    get_installation,
    list_installations,
    onboard,
    resubscribe,
    signup_config,
)
from app.crm.connectivity.queue import queue_message, send_behind
from app.crm.connectivity.reasons import reason_label
from app.crm.connectivity.receipts import consume_status_event
from app.crm.connectivity.schemas.connector import ConversationSettings
from app.crm.connectivity.schemas.message import TextBody
from app.crm.connectivity.session import send_session
from app.crm.connectivity.settings import (
    buddy_binding,
    conversation_settings,
    list_channel_settings,
    update_channel_settings,
)
from app.crm.connectivity.templates.events import consume_template_event
from app.crm.connectivity.templates.lifecycle import (
    create_draft as create_template_draft,
    edit as edit_template,
    retire as retire_template,
    submit as submit_template,
)
from app.crm.connectivity.templates.reads import (
    get as get_template,
    list_templates,
    template_status,
)
from app.crm.connectivity.templates.retire_guard import register_retire_guard
from app.crm.connectivity.topics import TOPIC_QUEUED

__all__ = [
    # the dispatcher role
    "claim_sends",
    "dispatch_send",
    # producing a send
    "queue_message",
    # and learning that a reply answers one of them
    "send_behind",
    # asking a connector to act (the walker's action square)
    "perform_action",
    "action_names",
    "validate_action_args",
    "ActionError",
    # connections
    "signup_config",
    "onboard",
    "get_installation",
    "list_installations",
    "disconnect",
    # the template registry
    "create_template_draft",
    "submit_template",
    "edit_template",
    "retire_template",
    "get_template",
    "list_templates",
    # the publish-time check (outreach asks)
    "template_status",
    "registers_templates_for",
    # the retire guard slot (worker_main fills)
    "register_retire_guard",
    # the template webhook consumer (worker_main registers)
    "consume_template_event",
    # webhook subscription recovery
    "resubscribe",
    # the read-side word for a stored reason (the row keeps the code).
    # reason_class stays module-local until something outside connectivity
    # imports it — alert rules read it off the log stream (#1014 lesson).
    "reason_label",
    # the inbound bay, for app/crm/api.py's one registration line
    "META_INGRESS",
    # free-form replies inside the customer-service window
    "send_session",
    "TextBody",
    "conversation_profile",
    "conversation_channels",
    # the receipts consumer (worker_main registers)
    "consume_status_event",
    # the topic of our own send's echo — outreach must never react to it
    "TOPIC_QUEUED",
    # the merchant's bindings: the template binding, Buddy's binding, settings
    "buddy_binding",
    "conversation_settings",
    "list_channel_settings",
    "update_channel_settings",
    "ConversationSettings",
]
