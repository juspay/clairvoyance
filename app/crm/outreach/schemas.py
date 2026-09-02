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

from pydantic import BaseModel, Field, model_validator


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
    # Repeat entries (modules/05-outreach §Repeat entries, sealed 31 Aug
    # 2026): what an OPEN run still on its first square does when another
    # entry event for the same key arrives. Words: ignore (default — the
    # unique absorbs it, today's behaviour) · refresh_latest ·
    # refresh_max(<payload field>) · accumulate. Vocabulary in code
    # (repeat.py), validated at publish.
    on_repeat: str = "ignore"
    # Every matching repeat slides the entry wait's alarm to now + this.
    debounce_minutes: float = Field(0, ge=0)


class WorkflowGoalKey(BaseModel):
    """What ties a goal letter to ONE run: the letter's payload field must
    equal the run's context field (cart_token = cart_token). Compared as
    text — keys are ids and tokens, never amounts."""

    event: str = Field(min_length=1)
    run: str = Field(min_length=1)


# The reasons a goal TIER may end a run with (vocabulary in code; the 063
# CHECK on the column is the closed superset). timed_out, ejected and
# completed are the walker's own verdicts, never a tier's.
GOAL_EXIT_REASONS = ("goal_met", "converted_elsewhere", "withdrawn")


class WorkflowGoal(BaseModel):
    """One goal TIER (rollout phase 06): the 'she did the thing' topics,
    optionally keyed to the run they are about, and the reason the run
    exits with. Tiers are judged keyed-first (goal_tiers()): the keyed
    tier says "THIS cart recovered" (goal_met); the unkeyed one says "she
    bought something" and still ends every other open run — never nudge
    someone who just bought — but as converted_elsewhere, so the funnel
    can tell the two apart. A single unkeyed tier is today's behaviour."""

    topics: List[str] = Field(min_length=1)
    key: Optional[WorkflowGoalKey] = None
    exit_reason: str = "goal_met"


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
    goals: List[WorkflowGoal] = Field(min_length=1)
    exits: WorkflowExits = Field(default_factory=WorkflowExits)
    # ADR 0023: what a publish does to runs in flight. pin (default) — new
    # entrants take the new version, runs in flight finish the one they
    # entered under; migrate — every open run is re-pinned to the new
    # version inside the publish atom, allowed only when the stranding
    # validator passes (057's semantics as an opt-in mode). Vocabulary in
    # code; the 064 CHECK on the stored column is the closed superset.
    on_publish: Literal["pin", "migrate"] = "pin"
    # What the plan's sends are for (canon T16 col 9, NOT NULL on the
    # manifest; the gate checks it against the grant). Required once the
    # plan has a send node; the send node copies it onto every row.
    purpose_key: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _goal_becomes_one_tier(cls, data: Any) -> Any:
        """The singular `goal` (every document published before phase 06)
        is one tier; `goals` is the list. Both at once is ambiguous."""
        if isinstance(data, dict) and "goal" in data:
            if "goals" in data:
                raise ValueError("give goal or goals, not both")
            data = dict(data)
            data["goals"] = [data.pop("goal")]
        return data

    def goal_tiers(self, topic: Optional[str] = None) -> List[WorkflowGoal]:
        """The tiers to judge, in judging order: keyed first (the more
        specific claim — a run it ends is no longer open for the unkeyed
        sweep), then unkeyed; document order within each. With a topic,
        only the tiers listening for it."""
        tiers = [t for t in self.goals if topic is None or topic in t.topics]
        return [t for t in tiers if t.key] + [t for t in tiers if not t.key]

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


class WorkflowRunSummary(BaseModel):
    """One plan's runs over a window (rollout phase 09, G9): how many
    started, how they ended, what is still in flight, how long they took,
    and what the recovered ones were worth. ``WorkflowSummary`` is the
    list shape, hence the name."""

    runs: int
    by_exit_reason: Dict[str, int]
    open: Dict[str, int]
    median_minutes_to_exit: Optional[float]
    recovered_amount: Optional[float]


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


class CustomerRun(EnrollmentRun):
    """A run as the customer's journey lists it — across every plan, so
    each row says which plan it belongs to (rollout phase 09)."""

    workflow_name: str
