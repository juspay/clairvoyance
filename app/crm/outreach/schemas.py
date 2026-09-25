"""Leaf shapes for the outreach module (module rules §1). Imports nothing
internal — db/decoder.py is the only place a row becomes one of these.

The definition models mirror canon T19's document sections exactly:
{entry, nodes, edges, goal, exits}. Pydantic checks SHAPE here; the graph
LAWS (unique node ids, edges reference real nodes, branching only out of
a listening wait or a condition/split) live in plans.validate_definition — a pure decide
function, testable without a database.
"""

from datetime import date, datetime
from typing import Any, Dict, List, Literal, NamedTuple, Optional, Tuple, Union
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

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
    """The run's ceilings: entered_at + max_age_days -> timed_out; and
    max_calls_per_day, the calls one run may place in a calendar day, after
    which a call square places none and the run walks on (phase 20).

    ``max_age_days`` defaults on the model. The call ceiling does NOT, and
    nothing fills it in anywhere: a board is bounded only when its author
    writes the word. The day is then the PLAN's, never the server's — the
    same reasoning ``WaitWindow`` states, since a wrong clock resets the
    count at the wrong hour."""

    # > 0 or every run times out on its first claim (now - entered_at is
    # always positive); rejected at model_validate, so publish refuses it.
    max_age_days: float = Field(7.0, gt=0)
    # Calls this run may place per CALENDAR DAY, over every call square and
    # every revisit (each visit mints its own lead since 967a86df). Counted
    # from the run's own day-stamped ledger (outreach/ceiling.py calls_today);
    # buddy's per-lead re-dials (call_execution_config.max_retry) are a
    # separate layer and compose multiplicatively.
    #
    # NOT defaulted, here or at the write path: definitions.py re-validates
    # the STORED document on every claim, so a default would cap runs in
    # flight under a version their author never capped (ADR 0023 §1/§5).
    # Nor is it a safe bend — a capped square dials nothing (its lead is
    # born ABORTED), and the walker answers a wait listening for that
    # call's report with ABORTED at once.
    # The bound is the author's word or nothing.
    max_calls_per_day: Optional[int] = Field(None, ge=1)
    # The clock the day is read on (IANA), REQUIRED whenever a ceiling is
    # named — WaitWindow.timezone below is required for this exact hazard:
    # a plan whose window says America/New_York would otherwise reset its
    # budget at 14:30 ET, mid calling-window.
    timezone: Optional[str] = None

    @model_validator(mode="after")
    def _a_daily_ceiling_names_its_clock(self) -> "WorkflowExits":
        if self.timezone is not None:
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError(f"exits: unknown timezone {self.timezone!r}")
        if self.max_calls_per_day is not None and not self.timezone:
            raise ValueError(
                "exits.max_calls_per_day needs exits.timezone — the day it "
                "counts is read on the plan's clock, and an unnamed one would "
                "reset the count at the server's midnight"
            )
        return self


class WorkflowMatch(BaseModel):
    """WHOSE letter a listening square hears (rollout phase 18): the
    letter's payload field must equal the run's field — `id` (the run's
    own id; a call's outcome names it as enrollment_id) or a context
    field (lead_<node>, message_<node>). A customer can have two runs;
    a letter about one call must never wake the other. Compared as text,
    the goal-key precedent; a letter without the field is about nobody."""

    payload: str = Field(min_length=1)
    run: str = Field(min_length=1)


# A clock time on a window, HH:MM on the 24-hour clock.
_HH_MM = r"^([01]\d|2[0-3]):[0-5]\d$"

# The retired listening word (ruled 17 Sep 2026): a `wait` with topics is
# what `wait_event` was. Stored version rows still say it and still parse
# (WorkflowNode reads it as a wait); publish refuses it in a new document.
RETIRED_WAIT_EVENT = "wait_event"


class WaitWindow(BaseModel):
    """The hours a waiting square's timer may fire in (the calling window,
    17 Sep 2026): `opens` and `closes` are HH:MM on the `timezone` clock,
    `closes` exclusive, and an `opens` later than `closes` spans midnight. A timer that
    ends outside the hours holds the run on its square, still listening,
    until the window next opens (outreach/window.py). A letter is never
    held: it moves the run the moment it lands, as it always did — so
    publish refuses a letter arrow from a windowed square that reaches a
    call without waiting (plans.py).

    A scheduling window on the PLAN's clock, not the customer's: the author
    names the timezone, and a wrong one calls at the wrong local hour. It is
    NOT the quiet-hours control (ADR 0018's customer-timezone gate, which
    voice is outside of — ADR 0010); the dialler's calling hours remain the
    check on every call."""

    opens: str = Field(pattern=_HH_MM)
    closes: str = Field(pattern=_HH_MM)
    timezone: str = Field(min_length=1)
    # The morning offset (ruled 22 Sep 2026, docs/crm/runbooks/morning-offset.md):
    # the first `held_runs_first_minutes` after `opens` belong to the runs the window
    # held overnight. They wake at the opening as always and their calls go
    # to the dialler at once; every timer this window governs that is SET
    # during those minutes — a customer entering at 09:20, a held run's gap
    # after its morning call — is pushed by the offset, so the day's live
    # customers do not queue behind the pile. 0 = no reserved period.
    held_runs_first_minutes: int = Field(0, ge=0)

    @model_validator(mode="after")
    def _a_window_that_opens(self) -> "WaitWindow":
        if self.opens == self.closes:
            raise ValueError("window: opens and closes must differ")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"window: unknown timezone {self.timezone!r}")
        return self


class WorkflowNode(BaseModel):
    """One square of the board. Vocabulary is code, not CHECKs:
    wait · send (channel + template, via connectivity) ·
    call (template_id, via buddy's lead machine — ADR 0010) ·
    action (connector + action + args: a connector DOES something for the
    run — see the three fields below) · condition · split.

    `wait` has three forms (ruled 17 Sep 2026; `wait_event` folded in),
    and `topics` is the discriminant: no topics = a plain timer; topics =
    the timer OR an event, whichever first — the branch taken is the edge
    whose `on` equals the event's payload[key], or "timeout"; either one
    may carry a `window`. `minutes` is optional: absent, a listening wait
    lasts the run's life (exits.max_age_days) and a bare window waits only
    for the hours; the window then applies to whatever that duration is
    (outreach/window.py). A wait with none of the three waits for nothing
    and is refused at publish. key: "$topic" (rollout phase 15)
    branches on the event's TOPIC instead — the edge's `on` is the topic
    string — so a stage board reads "she went to KYC" from the letter's
    name; $topic is the only $-word. An edge labelled "else" (phase 18)
    takes any answer the square did not name — the alarm too, when there
    is no "timeout" edge."""

    id: str = Field(min_length=1)
    # `wait_event` stays readable (stored version rows) and is read as a
    # listening `wait` before this Literal judges it.
    type: Literal["wait", "send", "call", "wait_event", "action", "condition", "split"]
    minutes: Optional[float] = None
    channel: Optional[str] = None
    template: Optional[str] = None
    template_id: Optional[str] = None
    topics: List[str] = Field(default_factory=list)
    key: Optional[str] = None
    # action: WHO acts and WHAT they do — the two words a plan may say, and
    # the only two. No URL, no credential, no transport: connectivity holds
    # the merchant's connection and decides how the write travels, so the
    # day a relay becomes a direct provider call, this document is unchanged.
    connector: Optional[str] = None
    action: Optional[str] = None
    # action: the action's OWN arguments, validated at publish against the
    # model the connector declares for it. Values may be "{placeholder}" and
    # are resolved from the run's facts at fire time.
    args: Dict[str, Any] = Field(default_factory=dict)
    # Phase 16: an optional stage label the square belongs to. It rides to
    # templates as current_stage ("you stopped at {current_stage}") — one
    # call template for a whole board. The stages ladder (phase 17) sets it
    # for every square it expands.
    stage: Optional[str] = Field(None, min_length=1)
    # Phase 18: only the letter about THIS run wakes the square.
    match: Optional[WorkflowMatch] = None
    # wait only: the hours the timer may fire in. Outside them the run
    # holds on the square until the window opens.
    window: Optional[WaitWindow] = None
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
    # condition only (enh A/01): the rules, judged in order; the first whose
    # conditions all hold names the edge, none -> the mandatory `else` edge.
    rules: List["ConditionRule"] = Field(default_factory=list)
    # split only (enh A/04): the shares, in document order. Percents are
    # whole and sum to 100, so every run takes exactly one arm and a split
    # needs no `else`.
    arms: List["SplitArm"] = Field(default_factory=list)
    # call only: which playbook blocks this square takes into its lead
    # payload. NOTHING IMPLICIT — a block nobody asks for is never
    # evaluated, never in a payload, never in a log (the send node's own
    # philosophy, where a blank names the fact it wants). A send names its
    # blocks on the right of `variables`, an action inside `args`.
    blocks: List[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _wait_event_is_a_listening_wait(cls, data: Any) -> Any:
        """`wait_event` (every document before 17 Sep 2026) is a `wait`
        with topics — lossless, it always carried them. Stored versions keep
        walking; publish refuses the old word in a new document (plans.py)."""
        if isinstance(data, dict) and data.get("type") == RETIRED_WAIT_EVENT:
            data = {**data, "type": "wait"}
        return data


# An arrow: [from, to] or [from, to, on]. `on` labels a branch out of a
# listening wait ("YES", "NO", a topic, "timeout") or a condition / split;
# every other node has one plain arrow.
WorkflowEdge = Union[Tuple[str, str], Tuple[str, str, str]]


class ConditionRule(BaseModel):
    """One arm of a condition square (enh A/01): the edge label it names and
    the conditions that must ALL hold for it — the door's own where-grammar
    (shared/predicate.Condition), over the fields outreach/predicates.py
    resolves (context.<key>, facts.<node>.<key>, customer.<column>,
    customer.attributes.<name>). OR is two rules. `else` and `timeout` are
    the walker's words, never a rule's."""

    on: str = Field(min_length=1)
    if_: List[Condition] = Field(alias="if", min_length=1)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _label_is_not_a_walker_word(self) -> "ConditionRule":
        if self.on in ("else", "timeout"):
            raise ValueError(
                f"a rule may not be labelled {self.on!r} — the walker owns it"
            )
        return self


#: Where a split square records the arm a run took (enh A/04). Spelled on
#: the document's own vocabulary, not inside the node, because two layers
#: read it and neither may import the other: nodes/split.py writes the key
#: and db/queries/enrollment.py groups the report on it.
SPLIT_PREFIX = "split_"


class SplitArm(BaseModel):
    """One arm of a split square (enh A/04): the edge label it names and
    the share of runs that take it. Whole percents only — the shares must
    sum to 100 (nodes/split.py), which is a statement about integers, and
    a fractional share would make it one about rounding."""

    on: str = Field(min_length=1)
    percent: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _label_is_not_a_walker_word(self) -> "SplitArm":
        if self.on in ("else", "timeout"):
            raise ValueError(
                f"an arm may not be labelled {self.on!r} — the walker owns it"
            )
        return self


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
    # The ladder sets `blocks` for every call square it mints, the way it
    # sets template_id — otherwise a stages board (the loan funnel) could
    # never use the playbook on the squares the expander owns.
    blocks: List[str] = Field(default_factory=list)


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
    it into the wait board: the author never draws the O(n²)
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


class PlaybookBlock(BaseModel):
    """One row of a block: the first row whose `when` holds names what is
    said — a line, or an ordered list of lines.

    `when` is the where-grammar over the condition square's field grammar,
    so a block reads exactly what a condition square can read and nothing
    more. The LAST row carries no `when`: that is the condition square's
    mandatory `else`, and it is what guarantees the agent never speaks a
    literal "{hook_line}" on a live call."""

    when: List[Condition] = Field(default_factory=list)
    say: Union[str, List[str]]

    @model_validator(mode="after")
    def _say_names_something(self) -> "PlaybookBlock":
        names = [self.say] if isinstance(self.say, str) else self.say
        if not names or any(not n or not n.strip() for n in names):
            raise ValueError("say names a line, or an ordered list of lines")
        return self


class Transform(BaseModel):
    """How one fact is rendered wherever a line spells it: the built-ins it
    passes through, and their arguments.

    ``params`` is ONE table for the pipeline, each function handed only the
    keywords it declares — the same shape a buddy template's
    expected_payload_schema uses, so a merchant learns it once."""

    function: List[str] = Field(default_factory=list)
    params: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("function", mode="before")
    @classmethod
    def _one_or_many(cls, raw: Any) -> Any:
        return [raw] if isinstance(raw, str) else raw


class Playbook(BaseModel):
    """The plan fills the agent's holes.

    The agent is an actor and its template is a script with holes in it.
    The words that fill them — the opening line, the walk, the lender's
    notes — are chosen HERE, from what the event said, and handed over
    finished. The agent never picks; its template is edited only to change
    how the agent behaves, never what it says.

    Three rules hold the shape together:

      1. Every piece of text lives in `lines`, ONCE. A block never contains
         a sentence, it names one — so rewording a step changes every walk
         that uses it, and adding a lender is its lines plus one `when` row
         per block.
      2. `say` is a name, or an ordered list of names. One name renders as
         that line's text; a list renders one "- name: text" per line, in
         order.
      3. `transform` says how a FACT renders, ONCE for the whole plan — a
         credit limit named in ten lines is one decision about how money is
         written, not ten, and every line keeps the plain {current_limit}
         its author wrote.

    It lives in the plan document, so publish copies it into the version
    row: a journey that started on script v3 keeps saying v3, and a wording
    fix reaches open runs only with `on_publish: migrate`. That is the
    property a live vendor registration cannot have."""

    lines: Dict[str, str] = Field(default_factory=dict)
    blocks: Dict[str, List[PlaybookBlock]] = Field(default_factory=dict)
    # fact -> how it renders wherever a line spells it, once for the whole
    # plan. The RENDERED TEXT only: `when` rows and the lead payload read
    # the fact itself, so a plan still branches on current_limit > 10000
    # while a line reads "50 thousand rupees".
    transform: Dict[str, Transform] = Field(default_factory=dict)

    @field_validator("transform", mode="before")
    @classmethod
    def _shorthand(cls, raw: Any) -> Any:
        """A name or a pipeline is the common case, written bare."""
        if not isinstance(raw, dict):
            return raw
        return {
            k: {"function": v} if isinstance(v, (str, list)) else v
            for k, v in raw.items()
        }


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
    # The words the agent says, chosen by the plan from the event
    # (modules/05-outreach §The playbook; canon T19 col 6). Pinned with the
    # document at publish, unlike a registration, which is live under open runs.
    playbook: Optional[Playbook] = None

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
    # The first door's topic, read off the stored document — the list's
    # "Starts on …" line. Nothing on this shape is computed on read (the
    # week's seen/matched counts were, and cost a 23 s scan per list open
    # for Flipkart on 21 Sep 2026).
    entry_topic: Optional[str] = None


class Workflow(WorkflowSummary):
    """Detail shape — carries both documents."""

    definition: Optional[Dict[str, Any]]
    draft: Optional[Dict[str, Any]]


class DayCount(BaseModel):
    """One day of a per-day series: ``day`` is the calendar date in the
    timezone the caller asked for."""

    day: date
    runs: int


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
    # enh A/04: runs per arm of each split square, {node: {arm: count}}.
    # Empty for every plan with no split — the experiment's own report,
    # read from the runs themselves so there is no counter to drift.
    by_split: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    # The console's list sparkline: runs that entered per day of the window,
    # in the caller's timezone. Days with none are absent, not zero.
    runs_per_day: List[DayCount] = Field(default_factory=list)
    # "Where open runs are": waiting + parked runs by the square they stand
    # on NOW — a present-tense count, so the window does not narrow it.
    open_by_node: Dict[str, int] = Field(default_factory=dict)


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
    # When the token landed on current_node (073, canon T26 col 19): the
    # ONLY place the current square's arrival lives until its step closes,
    # and therefore the `arrived_at` of the next flushed row. NULL for runs
    # that pre-date 073 — honest, never backfilled to entered_at.
    node_arrived_at: Optional[datetime] = None


class RunStep(BaseModel):
    """One square a run has BEEN on (canon T26).

    Also the shape of the square it stands on NOW: ``steps.timeline()``
    unions the closed rows with the open square, and ``left_at is None`` is
    what says "still here". The table's own column is NOT NULL — only
    closed steps are ever written."""

    node: str
    node_type: str
    arrived_at: Optional[datetime]
    left_at: Optional[datetime]
    arrived_by: str
    outcome: Optional[str] = None
    next_node: Optional[str] = None
    attempts: int = 1
    last_error: Optional[str] = None
    # What this square handed to a dispatcher — a lead for a call, a
    # manifest row for a send. Resolved by (node_type, dispatch_id); T26.
    dispatch_id: Optional[str] = None
    cut_short_by: Optional[UUID] = None
    workflow_version: Optional[int] = None
    # The letter this row is about, by name: the one the run ENTERED on (the
    # door row), the one that cut this square short, or the one that ended
    # the run (the closing row). Resolved on read from the ids the trail
    # keeps — record's event_topics — never stored twice.
    event_topic: Optional[str] = None


class RunRow(EnrollmentRun):
    """A run as the runs list shows it: which of this key's runs of the
    plan it is ("Run 3 of 3"), counted over every run the plan holds for
    the same enrollment_key — exited ones included, until retention
    sweeps them."""

    run_number: int = 1
    runs_for_key: int = 1


class RunAnchor(BaseModel):
    """The newest row the first page saw, in the list's own sort keys
    (entered_at DESC, id DESC). The server establishes it on page 1 and
    the client echoes it back on every later page — opaquely, so no
    consumer has to know what the rows are sorted by."""

    entered_at: datetime
    id: UUID


class WorkflowPage(BaseModel):
    """One page of a merchant's plans and how many there are in all. The
    total travels in the body, typed — the RunPage ruling: never in a
    header a cross-origin page reads as null."""

    items: List[WorkflowSummary]
    total: int


class RunPage(BaseModel):
    """One page of a plan's runs: the rows, the total matching the filters
    (and the anchor, when one bounds the set), and the anchor itself. The
    total travels in the body, typed, where OpenAPI and every client can
    see it — never in a header a cross-origin page reads as null."""

    items: List[RunRow]
    total: int
    anchor: Optional[RunAnchor] = None


class PublishCheck(BaseModel):
    """The publish laws run on the saved draft without publishing: empty
    ``problems`` means Publish would pass them (templates included).
    ``has_draft`` false = there is nothing to publish, so nothing to fix."""

    has_draft: bool
    problems: List[str]


class RunCall(BaseModel):
    """One call the run placed (a lead stamped with its enrollment_id)."""

    lead_id: str
    node: Optional[str]
    status: str
    outcome: Optional[str]
    next_attempt_at: Optional[datetime]
    call_initiated_time: Optional[datetime]
    call_end_time: Optional[datetime]
    duration_seconds: Optional[int]
    attempt_count: int
    template: Optional[str]
    call_id: Optional[str]
    cost: Optional[float]


class TemplateCalls(BaseModel):
    """One agent's share of a plan's calls. A plan may fire more than one
    template — the call square's own, and (PR #1156) an arm's — and "which
    agent got which outcome" is the question a merchant asks of a
    multi-template plan; a single plan-wide bar cannot answer it."""

    template: str
    placed: int
    connected: int
    by_outcome: Dict[str, int]
    talk_seconds_avg: Optional[float]
    attempts_avg: Optional[float]
    cost_total: Optional[float]


class RunEnding(NamedTuple):
    """How one run stands, as the report judges it: nothing more than the
    columns build_report reads. A leaf shape (this file), not a db type —
    the accessor fills it, the pure fold consumes it."""

    id: str
    enrollment_key: str
    status: str
    exit_reason: Optional[str]
    exited_at: Optional[datetime]
    entered_at: Optional[datetime] = None
    current_node: Optional[str] = None


class ReportReach(BaseModel):
    """How the runs that got THIS far ended. Three stages partition the
    runs — never dialled, dialled but nobody answered, spoken to — and
    within each the five endings partition the stage, so a flow chart
    drawn from these (Entered → stage → ending) is a true whole with no
    remainder invented on the way."""

    runs: int
    goal_met: int
    withdrawn: int
    ejected: int
    other_ended: int
    open: int


# The three stages of ReportCustomers.by_reach, in journey order.
REACH_STAGES = ("never_dialled", "dialled_no_answer", "spoke")


class ReportCallBar(BaseModel):
    """One bar of the calls-per-customer chart: the runs with exactly
    ``calls`` FINISHED call leads (placed or not), and how they ended
    (outcome → leads). The outcomes sum to calls × runs."""

    calls: int
    runs: int
    outcomes: Dict[str, int] = Field(default_factory=dict)


class ReportCustomers(BaseModel):
    """Customer level: one row per run that ENTERED the window. A run is a
    customer's journey through the plan, so these read as customers.

    ``reached`` = at least one ANSWERED call (placed, finished, outcome
    not NO_ANSWER / a carrier failure; BUSY — a picked-up line with no
    input — counts, ruled 24 Sep 2026). The before/after split is
    decided per run by time: the run's first answered call against its
    exited_at — "after" says a conversation preceded the end, never that
    it caused it.

    ``by_reach`` is the same runs cut the other way, keyed by REACH_STAGES:
    a run is "spoke" when a conversation preceded its end (or, still open,
    has happened), "dialled_no_answer" when it was rung but not spoken to,
    else "never_dialled" — so goal_met_after_reach IS by_reach["spoke"]
    .goal_met and goal_met_before_reach is the other two stages' sum.
    ``open_by_square`` says where the still-open runs stand right now
    (current_node → runs), so "still open" splits into waiting-for-a-call
    and waiting-for-an-event by the plan's own squares."""

    runs: int
    unique_customers: int
    # How many calls each run was placed → how many runs: {"0": never
    # dialled, "1": once, "2": twice, ...}. Only counts that occurred appear,
    # so "2+" or "5+" is the reader's sum and never a field to add here.
    calls_per_customer: Dict[str, int]
    reached: int
    goal_met_before_reach: int
    goal_met_after_reach: int
    withdrawn_before_reach: int
    withdrawn_after_reach: int
    ejected: int
    other_ended: int
    open: int
    by_reach: Dict[str, ReportReach] = Field(default_factory=dict)
    open_by_square: Dict[str, int] = Field(default_factory=dict)
    # The calls-per-customer chart: every FINISHED call lead, placed or not,
    # so a call the dialler refused (CALL_LIMIT_REACHED) is on it; one still
    # queued or on the line is not. calls_per_customer above stays
    # placed-only — "customers called twice or more" is about calls that rang.
    call_histogram: List[ReportCallBar] = Field(default_factory=list)


class ReportCalls(BaseModel):
    """Call level: every lead those runs minted. ``repeat_calls`` are
    placed calls beyond the first to a run; ``repeat_answered`` answered
    calls beyond the first — the second conversation with the same
    person. Rates are the reader's (a percentage is a presentation)."""

    leads: int
    placed: int
    answered: int
    no_answer: int
    busy: int
    in_progress: int
    repeat_calls: int
    repeat_answered: int


class ReportTemplate(BaseModel):
    """The call table, and the per-customer counts that depend on it,
    scoped to ONE agent — so a merchant reading a multi-template plan can
    look at each agent on its own. ``reached`` is runs this agent spoke
    to; ``calls_per_customer`` counts only this agent's calls."""

    template: str
    calls: ReportCalls
    reached: int
    calls_per_customer: Dict[str, int]
    call_histogram: List[ReportCallBar] = Field(default_factory=list)


class WorkflowReport(BaseModel):
    """The day report a merchant is shown: the two tables, from one
    window on entered_at, plus the call table again per agent that rang
    (busiest first). The plan-wide numbers are the sum of the cards, folded
    from the same read, so the two can never disagree."""

    customers: ReportCustomers
    calls: ReportCalls
    by_template: List[ReportTemplate] = Field(default_factory=list)


class WorkflowCallSummary(BaseModel):
    """The plan's calls over a window of its runs' entered_at: every lead
    its runs placed — stamped leads and their retries, the same set the
    report folds. connected = the line was picked up, judged by the lead
    store's one answered definition (placed, finished, outcome not
    NO_ANSWER / NUMBER_UNAVAILABLE / FAILED — BUSY counts since 24 Sep
    2026); answered is the same count, kept on the wire for the console.
    reached_runs / contacted_runs count runs the same way the report
    does."""

    placed: int
    connected: int
    answered: int
    by_outcome: Dict[str, int]
    # One card per agent that rang, busiest first — the totals above are
    # their sum, folded from the same read.
    by_template: List[TemplateCalls] = Field(default_factory=list)
    talk_seconds_avg: Optional[float]
    attempts_avg: Optional[float]
    contacted_runs: int
    reached_runs: int
    cost_total: Optional[float]


class CustomerRun(EnrollmentRun):
    """A run as the customer's journey lists it — across every plan, so
    each row says which plan it belongs to (rollout phase 09)."""

    workflow_name: str
