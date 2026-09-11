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
- ``provider_message_id_for`` — the provider's own id for a logical send a
  producer once proposed, by the producer's own dedupe_key. The reply join:
  a reply carries the provider's id for the message it answers and nothing
  else, so a listening square resolves WHOSE send it answers through this
  read. NULL until an attempt was accepted, and only ever the post-accept
  ladder — a message nobody received is not a correlate.
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

``send()`` stays OFF this surface so that nothing outside the module can
reach a provider without passing the checks in front of it. So does the
route resolver, and so do the provider packages.
"""

from app.crm.connectivity.actions import (
    action_names,
    perform_action,
    validate_action_args,
)
from app.crm.connectivity.channels import registers_templates_for
from app.crm.connectivity.connectors import ActionError
from app.crm.connectivity.dispatch import claim_sends, dispatch_send
from app.crm.connectivity.ingress import META_INGRESS
from app.crm.connectivity.onboarding import (
    disconnect,
    get_installation,
    list_installations,
    onboard,
    resubscribe,
)
from app.crm.connectivity.queue import provider_message_id_for, queue_message
from app.crm.connectivity.reasons import reason_label
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

__all__ = [
    # the dispatcher role
    "claim_sends",
    "dispatch_send",
    # producing a send
    "queue_message",
    # the reply join: a producer reads back the provider's id for a send it
    # proposed, by its own dedupe_key
    "provider_message_id_for",
    # asking a connector to act (the walker's action square)
    "perform_action",
    "action_names",
    "validate_action_args",
    "ActionError",
    # connections
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
    # the read-side word for a stored reason (the row keeps the code)
    "reason_label",
    # the inbound bay, for app/crm/api.py's one registration line
    "META_INGRESS",
]
