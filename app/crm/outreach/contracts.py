"""outreach module — public surface (module rules §1). The ONLY file other
modules (and app/crm/worker_main.py) may import from app/crm/outreach.
Logic-layer functions only, never accessors.

  consume_attributed_event  — the entry-rules CONSUMER: the event worker's
                              pass calls it per row, inside the row's
                              savepoint, before the row's stamp.
  claim_due_runs / walk_run — the walker's pair for the shared drain loop
                              (CRM_ROLE=walker).
  template_references       — who would still send a template: (open
                              runs by their pinned documents, live/paused
                              plans by their latest) — the guard
                              connectivity's retire asks, through the slot
                              worker_main fills, since connectivity may
                              not import this file.
"""

from app.crm.outreach.entry import consume_attributed_event
from app.crm.outreach.versions import template_references
from app.crm.outreach.workers import claim_due_runs, walk_run

__all__ = [
    "consume_attributed_event",
    "claim_due_runs",
    "walk_run",
    "template_references",
]
