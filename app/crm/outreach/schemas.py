"""Leaf shapes for the outreach module (module rules §1). Imports nothing
internal — db/decoder.py is the only place a row becomes one of these.

The definition models mirror canon T19's document sections exactly:
{entry, nodes, edges, goal, exits}. Pydantic checks SHAPE here; the graph
LAWS (unique node ids, edges reference real nodes, branching only out of
a wait_event node) live in plans.validate_definition — a pure decide
function, testable without a database.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from uuid import UUID

from pydantic import BaseModel, Field


class WorkflowEntry(BaseModel):
    """Which event admits a customer, and the admission guards enforced
    for both doors (canon: entry carries reenter + cooldown)."""

    topic: str = Field(min_length=1)
    # Optional payload condition: every key must equal (orders/create AND
    # payload.gateway = COD). Empty = topic alone admits.
    where: Dict[str, Any] = Field(default_factory=dict)
    reenter: bool = False
    cooldown_hours: float = Field(24.0, ge=0)
    # What a run is ABOUT (canon T20 col 13, ruled 31 Aug 2026): the payload
    # field whose value keys the open-run unique. Omitted = the customer id
    # (bursts coalesce: one abandonment conversation); "order_id" = one run
    # per order (WISMO: two parcels, two parallel threads).
    key: Optional[str] = Field(None, min_length=1)


class WorkflowGoal(BaseModel):
    """The 'she did the thing' topics: any one of them ends every open
    run for the customer (match=customer; keyed matching lands with the
    WISMO-style flows)."""

    topics: List[str] = Field(min_length=1)


class WorkflowExits(BaseModel):
    """The run's hard ceiling: entered_at + max_age_days -> timed_out."""

    # > 0 or every run times out on its first claim (now - entered_at is
    # always positive); rejected at model_validate, so publish refuses it.
    max_age_days: float = Field(7.0, gt=0)


class WorkflowNode(BaseModel):
    """One square of the board. Vocabulary is code, not CHECKs:
    wait (minutes) · send (channel + template, via connectivity) ·
    call (template_id, via buddy's lead machine — ADR 0010) ·
    wait_event (topics + key + minutes: waits for an event OR the timer,
    whichever first; the branch taken is the edge whose `on` equals the
    event's payload[key], or "timeout")."""

    id: str = Field(min_length=1)
    type: Literal["wait", "send", "call", "wait_event"]
    minutes: Optional[float] = None
    channel: Optional[str] = None
    template: Optional[str] = None
    template_id: Optional[str] = None
    topics: List[str] = Field(default_factory=list)
    key: Optional[str] = None


# An arrow: [from, to] or [from, to, on]. `on` labels a branch out of a
# wait_event node ("YES", "NO", "timeout"); every other node has one plain
# arrow.
WorkflowEdge = Union[Tuple[str, str], Tuple[str, str, str]]


class WorkflowDefinition(BaseModel):
    """THE plan, whole (canon T19). nodes[0] is the start square —
    explicit convention, stated here once. Node ids are minted by the
    author and never regenerated (the publish validator's first law)."""

    entry: WorkflowEntry
    nodes: List[WorkflowNode] = Field(min_length=1)
    edges: List[WorkflowEdge] = Field(default_factory=list)
    goal: WorkflowGoal
    exits: WorkflowExits = Field(default_factory=WorkflowExits)
    # What the plan's sends are for (canon T16 col 9, NOT NULL on the
    # manifest; the gate checks it against the grant). Required once the
    # plan has a send node; the send node copies it onto every row.
    purpose_key: Optional[str] = None

    def outgoing(self) -> Dict[str, List[Tuple[str, Optional[str]]]]:
        """node id -> [(next node id, on)], in document order."""
        table: Dict[str, List[Tuple[str, Optional[str]]]] = {}
        for edge in self.edges:
            src, dst = edge[0], edge[1]
            on = edge[2] if len(edge) == 3 else None
            table.setdefault(src, []).append((dst, on))
        return table


class WorkflowSummary(BaseModel):
    """List shape — the jsonb documents are never fetched for lists
    (the identity CrmCustomerSummary precedent)."""

    id: UUID
    merchant_id: str
    name: str
    status: str
    version: int
    created_by: Optional[str]
    created_at: datetime
    updated_at: datetime


class Workflow(WorkflowSummary):
    """Detail shape — carries both documents."""

    definition: Optional[Dict[str, Any]]
    draft: Optional[Dict[str, Any]]


class EnrollmentRun(BaseModel):
    """One person's run — the token (canon T20)."""

    id: UUID
    merchant_id: str
    workflow_id: UUID
    workflow_version: int
    customer_id: UUID
    status: str
    current_node: str
    wake_at: Optional[datetime]
    entered_at: datetime
    exited_at: Optional[datetime]
    exit_reason: Optional[str]
    context: Dict[str, Any]
    enrollment_key: str
    attempts: int
    last_error: Optional[str]
