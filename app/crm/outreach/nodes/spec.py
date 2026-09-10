"""What one word of the vocabulary must answer, and the failure a word
raises when it cannot.

Split from ``__init__`` so a word module can import the type it implements
without importing the registry that lists it — the registry imports the
words, and the reverse edge would be a cycle.
"""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.crm.outreach.schemas import EnrollmentRun, WorkflowDefinition, WorkflowNode

Validate = Callable[[WorkflowNode, WorkflowDefinition], List[str]]
#: The pure half of a branching square's execute: which label it names,
#: from facts alone. Used by the dry run (enh A/05), which has no customer
#: to read and no run to write — the walker still calls execute, because a
#: condition may pay for a customer read and a simulation may not.
Decide = Callable[[WorkflowNode, Dict[str, Any]], Optional[str]]
#: What a square WOULD do, for a dry run: the send it would post, the call
#: it would place, the action it would perform, with everything resolved
#: from the facts in hand. Never performs it.
Describe = Callable[[WorkflowNode, Dict[str, Any], WorkflowDefinition], Dict[str, Any]]
Execute = Callable[
    [EnrollmentRun, WorkflowNode, WorkflowDefinition], Awaitable[Dict[str, Any]]
]


class NodeParked(Exception):
    """A deterministic execution failure: parking is the honest outcome
    (a missing template, a module not yet deployed). Transient failures
    raise anything else and retry on the lease."""


@dataclass(frozen=True)
class NodeSpec:
    """What one word of the vocabulary must answer. execute is None for a
    wait: landing on it IS the action (the alarm), so is_wait and execute
    are two views of one fact and the registry test pins them together."""

    validate: Validate
    execute: Optional[Execute]
    is_wait: bool
    # enh A/01, N1 retired: nothing outside the registry matches a type
    # string. `branches`: the square's edges carry labels and pick_next
    # reads its answer from reply_<node> (wait_event, condition).
    # `listens`: the square hears a letter — the entry consumer wakes it
    # and `match` belongs to it (wait_event only).
    branches: bool = False
    listens: bool = False
    # enh A/05, both read ONLY by the dry run. `decide` is a branching
    # square's answer without the reads execute may make; `describe` is
    # what a firing square would send, resolved but never sent.
    decide: Optional[Decide] = None
    describe: Optional[Describe] = None
