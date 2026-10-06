"""outreach module — public surface (module rules §1). The ONLY file other
modules (and app/crm/worker_main.py) may import from app/crm/outreach.
Logic-layer functions only, never accessors.

  consume_attributed_event  — the entry-rules CONSUMER: the event worker's
                              pass calls it per row, inside the row's
                              savepoint, before the row's stamp.
  claim_due_runs / walk_run — the walker's pair for the shared drain loop
                              (CRM_ROLE=walker).
  register_call_rerank      — the dialler's hook for "this waiting call's
                              rank changed" (nodes/call.py); unset = no-op.
  template_references       — who would still send a template: (open
                              runs by their pinned documents, live/paused
                              plans by their latest) — the guard
                              connectivity's retire asks, through the slot
                              worker_main fills, since connectivity may
                              not import this file.
"""

from app.crm.outreach.entry import consume_attributed_event
from app.crm.outreach.nodes.call import register_call_rerank
from app.crm.outreach.versions import template_references
from app.crm.outreach.workers import claim_due_runs, walk_run

__all__ = [
    "consume_attributed_event",
    "claim_due_runs",
    "walk_run",
    "register_call_rerank",
    "template_references",
]
