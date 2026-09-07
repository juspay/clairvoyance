"""The node vocabulary — ONE registry (modules/05-outreach, ruled 31 Aug
2026): every square the board speaks is one entry here, carrying the three
things a type must answer — how the publish validator checks it, what the
walker does when the token lands on it, and whether landing on it means
waiting. The validator iterates NODE_TYPES, the walker dispatches through
it, enrol() asks it for the first alarm; none of them matches type
strings. Adding a type = one module + one entry + one word in the schema's
Literal, and a test pins the last two together so a half-added type fails
CI instead of shipping a walker that doesn't speak what the validator
accepted.

ONE WORD PER FILE, and the registry ASSEMBLED here from them — the
record/extractors/__init__.py (SPEC_MODULES) precedent, which is the
sanctioned shape for a non-empty __init__. It replaced a single module
that reached 596 lines carrying five validators, three executors, the
placeholder helpers, the run-context filters and the registry, at which
point "where does a call decide it cannot proceed?" needed a search rather
than a filename (modules/00 §1: the split lands in the PR that crosses
the line, and the fifth word is what crossed it).

  wait.py        time passes; the alarm was set on arrival.
  wait_event.py  the alarm OR an event, whichever first (W5).
  call.py        a buddy lead into today's dispatch machine (ADR 0010).
  send.py        one manifest row, queued, no verdict (gate-mechanics §1).
  action.py      a connector DOES one thing for this run.
  context.py     what in a run's context is OURS — the one filter the call
                 payload and the send variables both derive from.
  spec.py        NodeSpec and NodeParked: what a word must answer, and how
                 it says it cannot.

Every name the twelve importers used from the old module is re-exported
below, so the split is a move and not a migration.
"""

from typing import Dict

from app.crm.outreach.nodes import action, call, send, wait, wait_event
from app.crm.outreach.nodes.action import execute as execute_action
from app.crm.outreach.nodes.call import execute as execute_call
from app.crm.outreach.nodes.context import (
    _BOOKKEEPING_KEYS,
    _BOOKKEEPING_PREFIXES,
    _REQUEST_ID_KEYS,
    LATEST_LETTER_KEY,
    lead_request_id,
    reply_key,
    run_facts,
    send_variables,
    without_reply,
)
from app.crm.outreach.nodes.send import execute as execute_send
from app.crm.outreach.nodes.spec import Execute, NodeParked, NodeSpec, Validate
from app.crm.outreach.nodes.wait_event import ELSE, TIMEOUT, TOPIC_KEY
from app.crm.outreach.schemas import WorkflowNode

NODE_TYPES: Dict[str, NodeSpec] = {
    "wait": NodeSpec(validate=wait.validate, execute=None, is_wait=True),
    "send": NodeSpec(validate=send.validate, execute=send.execute, is_wait=False),
    "call": NodeSpec(validate=call.validate, execute=call.execute, is_wait=False),
    "wait_event": NodeSpec(validate=wait_event.validate, execute=None, is_wait=True),
    "action": NodeSpec(validate=action.validate, execute=action.execute, is_wait=False),
}


def is_wait(node: WorkflowNode) -> bool:
    """The one place that answers "does landing here mean waiting?" —
    enrol()'s first alarm, the walker's step and its next alarm all ask
    this; none may match a type string."""
    return NODE_TYPES[node.type].is_wait


__all__ = [
    "ELSE",
    "Execute",
    "LATEST_LETTER_KEY",
    "NODE_TYPES",
    "NodeParked",
    "NodeSpec",
    "TIMEOUT",
    "TOPIC_KEY",
    "Validate",
    "_BOOKKEEPING_KEYS",
    "_BOOKKEEPING_PREFIXES",
    "_REQUEST_ID_KEYS",
    "execute_action",
    "execute_call",
    "execute_send",
    "is_wait",
    "lead_request_id",
    "reply_key",
    "run_facts",
    "send_variables",
    "without_reply",
]
