"""connectivity — the public surface.

The only file other modules and app/crm/worker_main.py may import.

This module owns everything between "we want to send something" and "the
provider took it": connector accounts, the endpoints under them, templates,
the message table, send() and the dispatch pass. It is channel-agnostic —
WhatsApp, Instagram and email are adapters behind send(), not packages.

claim_sends/dispatch_send are the module's two callables for the shared
drain-loop scaffold (design/worker-runtime.md): worker_main registers them
as the "dispatcher" role. queue_message() is how a producer (the walker's
send node first) proposes a send: one queued row, no verdict. send() stays
off this surface so that nothing outside can reach a provider.
"""

from app.crm.connectivity.dispatch import claim_sends, dispatch_send
from app.crm.connectivity.queue import queue_message

__all__ = ["claim_sends", "dispatch_send", "queue_message"]
