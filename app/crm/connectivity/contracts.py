"""connectivity — the public surface.

The only file other modules and app/crm/worker_main.py may import.

This module owns everything between "we want to send something" and "the
provider took it": connector accounts, the endpoints under them, templates,
the message table, send() and the dispatch pass. It is channel-agnostic —
WhatsApp, Instagram and email are adapters behind send(), not packages.

claim_sends/dispatch_send are the module's two callables for the shared
drain-loop scaffold (design/worker-runtime.md): worker_main registers them
as the "dispatcher" role. claim_sends is workers.py's wrapper, not
dispatch.py's raw claim — it folds the periodic WhatsApp template sync in
as housekeeping on the same loop (one pod, one loop). queue_message() is
how a producer (the walker's send node first) proposes a send: one queued
row, no verdict.
onboard_whatsapp/get_installation/list_installations/disconnect manage
connector accounts and their bindings (the send path resolves its own
route through send.py, which is why no binding getter sits on this
surface);
create_template_draft/submit_template/edit_template/retire_template/
get_template/list_templates own the template registry that dispatch's
crm_message.template_id will eventually resolve against — its periodic
sync runs only inside claim_sends now, not as a standalone entry point.
send() stays off this surface so that nothing outside can reach a provider.
"""

from app.crm.connectivity.dispatch import dispatch_send
from app.crm.connectivity.onboarding import (
    disconnect,
    get_installation,
    list_installations,
    onboard_whatsapp,
)
from app.crm.connectivity.queue import queue_message
from app.crm.connectivity.templates import (
    create_draft as create_template_draft,
    edit as edit_template,
    get as get_template,
    list_templates,
    retire as retire_template,
    submit as submit_template,
)
from app.crm.connectivity.workers import claim_sends

__all__ = [
    "claim_sends",
    "dispatch_send",
    "queue_message",
    "onboard_whatsapp",
    "get_installation",
    "list_installations",
    "disconnect",
    "create_template_draft",
    "submit_template",
    "edit_template",
    "retire_template",
    "get_template",
    "list_templates",
]
