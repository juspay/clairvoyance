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
