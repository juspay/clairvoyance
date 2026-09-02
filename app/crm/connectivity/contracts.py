"""connectivity — the public surface.

The only file other modules and app/crm/worker_main.py may import.

This module owns everything between "we want to send something" and "the
provider took it": connector accounts, the endpoints under them, the message
table, send() and the dispatch pass. It is channel-agnostic — WhatsApp,
Instagram and email are adapters and faces behind a registry, not packages
other modules know about.

What is here, and why each thing is on the surface:

- ``claim_sends`` / ``dispatch_send`` — the dispatcher role's two callables
  for the shared drain-loop scaffold (design/worker-runtime.md). The
  dispatcher sends, and only sends: no other work rides its loop.
- ``queue_message`` — how a producer (the walker's send node first) proposes
  a send: one queued row, no verdict.
- ``onboard`` / ``get_installation`` / ``list_installations`` / ``disconnect``
  — connector accounts and the pipes under them. Connector-agnostic:
  ``onboard`` takes a connector_key and a payload, and the CONNECTORS
  registry decides what that payload means.

``send()`` stays OFF this surface so that nothing outside the module can
reach a provider without passing the checks in front of it. So does the
route resolver, and so do the provider packages.
"""

from app.crm.connectivity.dispatch import claim_sends, dispatch_send
from app.crm.connectivity.onboarding import (
    disconnect,
    get_installation,
    list_installations,
    onboard,
)
from app.crm.connectivity.queue import queue_message

__all__ = [
    # the dispatcher role
    "claim_sends",
    "dispatch_send",
    # producing a send
    "queue_message",
    # connections
    "onboard",
    "get_installation",
    "list_installations",
    "disconnect",
]
