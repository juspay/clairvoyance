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

from app.crm.shared.predicate import Condition, from_equality_map


class WorkflowEntry(BaseModel):
    """Which event admits a customer, and the admission guards enforced
    for both doors (canon: entry carries reenter + cooldown)."""

    topic: str = Field(min_length=1)
    # Typed conditions, ANDed (design/event-catalog.md §The where-grammar):
    # [{field: "payload.gateway", op: "is", value: "COD"}]. Every field must
    # be declared in the catalog (code or registered layer) — the publish
    # validator refuses the rest. Empty = topic alone admits. The pre-catalog
    # equality map is retired: migration 069 rewrote every stored plan, and a
    # map that still reaches this model (a crm_workflow_version row is
    # immutable, so 069 could not touch it) is read as the conditions it
    # meant — the publish validator refuses a NEW document that writes one.
    where: List[Condition] = Field(default_factory=list)
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
    # Phase 16 (G8): a repeat of this door's topic re-arms the run's CURRENT
    # square, not only the start square — "KYC retried, the timer restarts"
    # — sliding its alarm by debounce_minutes and merging the facts. Needs
    # debounce_minutes > 0 (validated), or there is nothing to re-arm.
    restart_on_repeat: bool = False

    @model_validator(mode="before")
    @classmethod
    def _legacy_where_map(cls, data: Any) -> Any:
        """The pre-catalog equality map, read as the conditions it meant."""
        if isinstance(data, dict) and isinstance(data.get("where"), dict):
            data = {**data, "where": from_equality_map(data["where"])}
        return data


class WorkflowEntryAt(WorkflowEntry):
    """One DOOR of a plan (rollout phase 15): the entry words plus the
    square a run admitted through it starts on. A plan lists its doors
    when a journey may first be seen at any stage — a customer who
    appears at KYC starts on the KYC square, not on nodes[0]. The single
    `entry` object is one door starting on nodes[0]."""

    start: str = Field(min_length=1)


# The admission words a plan may state ONCE at the top level for every
# door (reenter, cooldown_hours, key, on_repeat, debounce_minutes); a door
# may still say its own. Folded into each door before validation.
_SHARED_ENTRY_WORDS = (
    "reenter",
    "cooldown_hours",
    "key",
    "on_repeat",
    "debounce_minutes",
    "restart_on_repeat",
)


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


class WorkflowMatch(BaseModel):
    """WHOSE letter a listening square hears (rollout phase 18): the
    letter's payload field must equal the run's field — `id` (the run's
    own id; a call's outcome names it as enrollment_id) or a context
    field (lead_<node>, message_<node>). A customer can have two runs;
    a letter about one call must never wake the other. Compared as text,
    the goal-key precedent; a letter without the field is about nobody."""

    payload: str = Field(min_length=1)
    run: str = Field(min_length=1)


class WorkflowNode(BaseModel):
    """One square of the board. Vocabulary is code, not CHECKs:
    wait (minutes) · send (channel + template, via connectivity) ·
    call (template_id, via buddy's lead machine — ADR 0010) ·
    wait_event (topics + key + minutes: waits for an event OR the timer,
    whichever first; the branch taken is the edge whose `on` equals the
    event's payload[key], or "timeout"). key: "$topic" (rollout phase 15)
    branches on the event's TOPIC instead — the edge's `on` is the topic
    string — so a stage board reads "she went to KYC" from the letter's
    name; $topic is the only $-word. An edge labelled "else" (phase 18)
    takes any answer the square did not name — the alarm too, when there
    is no "timeout" edge."""

    id: str = Field(min_length=1)
    type: Literal["wait", "send", "call", "wait_event"]
    minutes: Optional[float] = None
    channel: Optional[str] = None
    template: Optional[str] = None
    template_id: Optional[str] = None
    topics: List[str] = Field(default_factory=list)
    key: Optional[str] = None
    # Phase 16: an optional stage label the square belongs to. It rides to
    # templates as current_stage ("you stopped at {current_stage}") — one
    # call template for a whole board. The stages ladder (phase 17) sets it
    # for every square it expands.
    stage: Optional[str] = Field(None, min_length=1)
    # Phase 18: only the letter about THIS run wakes the square.
    match: Optional[WorkflowMatch] = None
    # send only: which run fact fills which template blank, {blank: fact}.
    # Left = the parameter the provider's registered template declares
    # ({{customer_name}} named, or "1"/"2" positional); right = the key in
    # the run's context — a variable field the catalog declares for the
    # entry topic (design/event-catalog.md: template variables ONLY from
    # declared fields; the publish validator refuses the rest). Absent or
    # empty = the template has no blanks and ZERO parameters are posted.
    # Never the whole context: crm_message.variables is what we actually
    # posted (canon T16 col 11), and a template with two blanks handed 27
    # facts is refused by every provider.
    variables: Dict[str, str] = Field(default_factory=dict)


# An arrow: [from, to] or [from, to, on]. `on` labels a branch out of a
# wait_event node ("YES", "NO", "timeout"); every other node has one plain
# arrow.
WorkflowEdge = Union[Tuple[str, str], Tuple[str, str, str]]


class StageAction(BaseModel):
    """What a stage does when it goes quiet (rollout phase 17): the action
    square minus the id and the stage label the expander mints — a call
    (template_id) or a send (channel + template). The square's own laws
    judge it once expanded (nodes.py): a send still needs the plan's
    purpose_key, and an approved template at publish."""

    type: Literal["call", "send"]
    template_id: Optional[str] = None
    channel: Optional[str] = None
    template: Optional[str] = None


class StageOverride(BaseModel):
    """One stage's own clocks or action, where they differ from the
    ladder's."""

    idle_minutes: Optional[float] = Field(None, gt=0)
    on_idle: Optional[StageAction] = None
    after_action_minutes: Optional[float] = Field(None, gt=0)


class Stages(BaseModel):
    """The ladder (rollout phase 17; notes §16.2): an ordered funnel of
    stage topics, one clock for "went quiet on a stage", one action when
    it fires, one listening window after the action. ladder.py expands
    it into the wait_event board: the author never draws the O(n²)
    arrows and the walker never sees the word. Shape only here — the
    expansion's laws (distinct square names, nothing hand-drawn beside
    the ladder) are the expander's."""

    order: List[str] = Field(min_length=2)
    idle_minutes: float = Field(gt=0)
    on_idle: StageAction
    after_action_minutes: float = Field(gt=0)
    # Phase 16's door word, set on every door the ladder mints: a stage's
    # own letter, repeated, re-arms whatever square the run stands on.
    restart_on_repeat: bool = False
    overrides: Dict[str, StageOverride] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _stages_are_distinct_and_overrides_name_one(self) -> "Stages":
        seen = set()
        for topic in self.order:
            if not topic:
                raise ValueError("order: a stage topic cannot be empty")
            if topic in seen:
                raise ValueError(f"order: stage {topic!r} appears twice")
            seen.add(topic)
        for topic in self.overrides:
            if topic not in seen:
                raise ValueError(f"overrides: {topic!r} is not a stage in order")
        return self


class WorkflowDefinition(BaseModel):
    """THE plan, whole (canon T19). Node ids are minted by the author and
    never regenerated (the publish validator's first law).

    `entry` is one door (an object — its run starts on nodes[0], the
    explicit convention stated here once) or a LIST of doors (phase 15),
    each naming its topic and the square its run starts on; `entries`
    is always the list."""

    entry: Union[WorkflowEntry, List[WorkflowEntryAt]]
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
    # Rollout phase 17: the ladder this board was expanded from, kept
    # beside what it produced (nodes/edges/entry filled by
    # ladder.expand_stages) so the console can re-edit the funnel. The
    # validator refuses a ladder with a hand-drawn board beside it.
    stages: Optional[Stages] = None

    def send_templates(self) -> List[Tuple[str, str]]:
        """PURE: every (channel, template name) a send node of this document
        names — what a run pinned to it may send, and so what the pinning
        paths lock shared against a retirement (phase 14)."""
        return [
            (node.channel, node.template)
            for node in self.nodes
            if node.type == "send" and node.channel and node.template
        ]

    # What the plan's sends are for (canon T16 col 9, NOT NULL on the
    # manifest; the gate checks it against the grant). Required once the
    # plan has a send node; the send node copies it onto every row.
    purpose_key: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _doors_take_the_shared_words(cls, data: Any) -> Any:
        """The admission words stated once at the top level reach every
        door; a door's own word wins (phase 15)."""
        if isinstance(data, dict) and any(w in data for w in _SHARED_ENTRY_WORDS):
            data = dict(data)
            shared = {w: data.pop(w) for w in _SHARED_ENTRY_WORDS if w in data}
            entry = data.get("entry")
            if isinstance(entry, list):
                data["entry"] = [
                    {**shared, **door} if isinstance(door, dict) else door
                    for door in entry
                ]
            elif isinstance(entry, dict):
                data["entry"] = {**shared, **entry}
        return data

    @property
    def entries(self) -> List[WorkflowEntryAt]:
        """The doors, in document order, one per topic. A single `entry`
        is one door starting on nodes[0]."""
        if isinstance(self.entry, list):
            return list(self.entry)
        return [WorkflowEntryAt(**self.entry.model_dump(), start=self.nodes[0].id)]

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
    # Drift observability (event-catalog.md §Seen vs matched): events on
    # the entry topic vs runs started, last 7 days — computed on read.
    entry_topic: Optional[str] = None
    seen_7d: int = 0
    matched_7d: int = 0


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


class WorkflowVersion(BaseModel):
    """One published document of a plan (ADR 0023) and how many open runs
    still execute it — the versions list (rollout phase 14)."""

    version: int
    on_publish: str
    published_by: Optional[str]
    published_at: datetime
    open_runs: int


class VersionMigration(BaseModel):
    """What a migrate-forward did: every open run that was pinned to
    from_version now executes to_version."""

    from_version: int
    to_version: int
    moved: int


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
