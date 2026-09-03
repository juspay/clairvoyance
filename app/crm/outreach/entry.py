"""The entry-rules consumer (W4 + W5) — outreach's subscription on the
event spine. One consumer, two reads (ADR 0023 §4; the sentence in
docs/crm/workflow-rollout/context/reading-notes.md §15.3):

  1. HER OPEN RUNS, each judged by the version it entered under
     (definitions.py): a goal tier of THAT document ends the run
     (goal-cancel); a wait_event square of THAT document wakes it with
     the answer written in its context (W5). A v3 run is ended by v3's
     goals and woken by v3's listening even after v5 changed them, and
     every write names the run it is about. A migrate plan is the
     degenerate case — every open run pinned to the latest — same path,
     no branch.
  2. THE LIVE PLANS, latest document: a door match (phase 15: a plan
     lists its doors, one per topic, each naming the square its run
     starts on) starts a run (enrol()) — a new run always begins on the
     newest version.

A consumer is a function, not a loop (worker-runtime.md): the event
worker's pass calls consume_attributed_event() per row, inside that row's
savepoint, before its stamp. A raise here rolls back only that row, which
stays pending and returns next poll. Our writes commit on their own
(enrol() opens its own atom), so a replay is safe by idempotency — the
source-event check and the open-run unique — not by that rollback.
"""

from typing import Optional, Sequence, Tuple

from app.core.logger import logger
from app.crm.outreach.db.accessors import (
    enrollment as enrollment_accessor,
    workflow as workflow_accessor,
)
from app.crm.outreach.definitions import definition_for
from app.crm.outreach.enrol import enrol
from app.crm.outreach.nodes import (
    _BOOKKEEPING_KEYS,
    _BOOKKEEPING_PREFIXES,
    LATEST_LETTER_KEY,
    TOPIC_KEY,
    reply_key,
)
from app.crm.outreach.repeat import _as_number, apply_repeat
from app.crm.outreach.schemas import (
    EnrollmentRun,
    Workflow,
    WorkflowDefinition,
    WorkflowEntry,
    WorkflowEntryAt,
    WorkflowNode,
)
from app.crm.record.contracts import RawEvent
from app.crm.shared.normalize import normalize_phone

# Fallback only: where a phone hides in a payload when no extractor
# handles were passed in (the voice mirrors, which send the flat shape).
# An external door's letter arrives VERBATIM under the two-plane ruling,
# so its phone is wherever that provider puts it — which is the source's
# extractor's business, not this file's.
_PHONE_PATHS = ("customer_mobile_number", "phone")

# The founding letter's own pointers: written once at enrol, never moved
# by a repeat (phase 00) — the id is what source_event_used dedupes on,
# the time is what "did she buy AFTER the run began" is measured from
# (G7): an order placed between the founding checkout and a later cart
# update must keep counting as after.
_FOUNDING_KEYS = ("source_event_id", "entered_event_at")

# Small-facts cap (canon: context carries pointers "plus the few small
# facts the sends will need" — never payload photocopies): scalars only,
# short values only; the full letter already lives on the event row.
_CONTEXT_VALUE_MAX_CHARS = 256


def _phone_from_payload(payload: dict) -> str | None:
    """The number the sends will actually dial or message — normalized to
    E.164 here, because resolve() normalizes only what it probes on and
    context is a separate copy. Unnormalized, a bare "9876543210" would
    resolve to +919876543210 for identity while the call node dialled the
    bare form, and a suppression stored in E.164 would not match it."""
    raw: str | None = None
    for key in _PHONE_PATHS:
        if payload.get(key):
            raw = str(payload[key])
            break
    if raw is None:
        customer = payload.get("customer")
        if isinstance(customer, dict) and customer.get("phone"):
            raw = str(customer["phone"])
    if raw is None:
        return None
    return normalize_phone(raw) or raw


async def consume_attributed_event(
    event: RawEvent, customer_id: Optional[str], handles: Optional[dict] = None
) -> None:
    """Match one just-attributed event against her open runs (each by its
    own version) and every live plan's entry (latest). customer_id
    arrives separately: the row object still carries the pre-stamp
    value — and is None for a merchant-level letter (a template review,
    an account notice), which has no person to start, end or wake a run
    for: outreach's business begins where a customer does.

    Order: every open run first — its goal, then its reply — and entries
    last. An order arriving right behind its checkout must not cancel the
    run that checkout is about to start; a run a goal just ended is not
    woken by the same letter; and a letter a run's square answered is not
    also that run's repeat (_answered_by).

    ``handles`` is what the source's extractor already found. Taking it
    rather than re-reading the payload keeps ONE source-aware discovery in
    the system: the number the sends dial is then the same number identity
    resolved on, so suppression matches by construction and a new source
    needs no teaching here. The payload search stays as the fallback for
    the voice mirrors, which resolve before this consumer exists."""
    if customer_id is None:
        return  # not about a person: nothing to admit, end or wake
    open_runs = await enrollment_accessor.open_runs_for_customer(
        event.merchant_id, customer_id
    )
    goal_patch = _goal_patch(event) if open_runs else None
    for run in open_runs:
        definition = await definition_for(run)
        if definition is None:
            # No version row for the pin: nothing honest can be judged for
            # this run here; the walker parks it at its next claim.
            logger.warning(
                f"run {run.id}: definition v{run.workflow_version} missing — "
                f"goals and listening not judged (event {event.id})"
            )
            continue
        if await _end_on_goal(run, definition, event, goal_patch):
            continue  # exited: there is nothing left to wake
        await _wake_on_reply(run, definition, event)

    flows = await workflow_accessor.live_workflows(event.merchant_id)
    for flow in flows:
        definition = WorkflowDefinition.model_validate(flow.definition)
        for door in definition.entries:
            if door.topic == event.topic and _where_matches(door.where, event.payload):
                await _try_enrol(
                    flow, definition, door, event, customer_id, handles, open_runs
                )
                break  # topics are unique across a plan's doors


async def _end_on_goal(
    run: EnrollmentRun,
    definition: WorkflowDefinition,
    event: RawEvent,
    goal_patch: Optional[dict],
) -> bool:
    """Judge this run against ITS document's goal tiers, keyed-first
    (goal_tiers, phase 06): "THIS cart recovered" (goal_met) beats "she
    bought something" (converted_elsewhere), and the first tier that ends
    the run is its verdict — no second tier sweeps an exited run. A keyed
    tier whose payload field is missing cannot say which run it is about
    and is skipped; one naming another run of hers is not this run's.
    Time-aware on the founding letter (G7) in the statement. Returns True
    when the run ended."""
    for tier in definition.goal_tiers(event.topic):
        key: Optional[Tuple[str, str]] = None
        if tier.key:
            value = event.payload.get(tier.key.event)
            if value in (None, ""):
                continue
            if str(run.context.get(tier.key.run, "")) != str(value):
                continue  # about another run of hers
            key = (tier.key.run, str(value))
        if await enrollment_accessor.cancel_run(
            run.merchant_id,
            str(run.id),
            tier.exit_reason,
            event.occurred_at,
            key,
            goal_patch,
        ):
            return True
    return False


async def _wake_on_reply(
    run: EnrollmentRun, definition: WorkflowDefinition, event: RawEvent
) -> None:
    """A wait_event square of ITS document listening on this topic wakes
    the run with the answer — the statement decides whether the token is
    standing there (a reply to a square it has left, or not yet reached,
    changes nothing), waiting or parked (phase 16: an event is evidence
    the customer moved). The letter's scalar facts ride along under the
    square (context.facts.<square>), so a later call can say what this
    stage's letter said; the same bridge enrol uses, so bookkeeping names
    and nested payload never reach the run."""
    facts = _context_from_payload(event.payload)
    for node in definition.nodes:
        if node.type != "wait_event" or event.topic not in node.topics:
            continue
        if not _is_about(node, event, run):
            continue  # another run's letter (phase 18): not this square's
        answer = _answer_for(node, event)
        if answer is None:
            # B1 (rollout phase 01): no key, no answer to branch on. Waking
            # the run with {reply_<node>: None} made pick_next read "the
            # alarm fired" and take the timeout edge at once — any letter
            # on the listened topic without the key ended the listening
            # window early. The window simply continues; only the alarm
            # may time it out.
            logger.info(
                f"wait_event reply ignored: key {node.key!r} missing "
                f"(run {run.id}, event {event.id})"
            )
            continue
        # The answer, and which square heard the latest letter — so the
        # facts of THIS letter win the next call even after the run has
        # moved on (nodes.run_facts; a ladder hears a stage's letter on
        # the square it leaves).
        await enrollment_accessor.resume_run_by_id(
            run.merchant_id,
            str(run.id),
            node.id,
            {reply_key(node.id): answer, LATEST_LETTER_KEY: node.id},
            facts,
        )


def _is_about(node: WorkflowNode, event: RawEvent, run: EnrollmentRun) -> bool:
    """PURE: is this letter about THIS run, as the square's `match` asks
    (phase 18)? The letter's field against the run's own id or a context
    field, as text (the goal-key precedent). No match word = every
    letter on the topic is hers; a letter without the field claims
    nobody, so it is not hers either."""
    if node.match is None:
        return True
    claimed = event.payload.get(node.match.payload)
    if claimed is None:
        return False
    mine = str(run.id) if node.match.run == "id" else run.context.get(node.match.run)
    return mine is not None and str(claimed) == str(mine)


def _answer_for(node: WorkflowNode, event: RawEvent) -> Optional[str]:
    """PURE: what this letter answers on this square — the topic itself
    for a $topic square (phase 15: the branch is the letter's NAME), else
    the payload field the square branches on; None when the square is not
    listening for the topic, or the field is missing (B1). The ONE
    definition of "this letter is this square's answer": the wake and
    the repeat refusal below both ask it."""
    if node.type != "wait_event" or event.topic not in node.topics:
        return None
    answer = event.topic if node.key == TOPIC_KEY else event.payload.get(node.key or "")
    return None if answer is None else str(answer)


async def _answered_by(
    open_runs: Sequence[EnrollmentRun],
    flow: Workflow,
    enrollment_key: str,
    event: RawEvent,
) -> bool:
    """Is this letter the answer the open run's CURRENT square listens for
    (by ITS version)? Then _wake_on_reply moved the run above, and the
    refused enrol is not a repeat. Judged before apply_repeat because a
    door that says restart_on_repeat patches the run on any square: it
    would push the alarm the wake just set (now) back by the debounce, and
    the token would sit on a square it has already answered — on a stages
    ladder (phase 17), where every stage is a door AND every earlier
    square listens for it, every stage clock would run twice."""
    for run in open_runs:
        if (
            str(run.workflow_id) == str(flow.id)
            and run.enrollment_key == enrollment_key
        ):
            pinned = await definition_for(run)
            if pinned is None:
                return False
            square = next((n for n in pinned.nodes if n.id == run.current_node), None)
            return (
                square is not None
                and _is_about(square, event, run)
                and _answer_for(square, event) is not None
            )
    return False


# Where an order's amount lives in a payload, most specific first —
# Shopify's total_price, the generic amount.
_AMOUNT_KEYS = ("total_price", "amount")


def _goal_patch(event: RawEvent) -> dict:
    """PURE: what a run keeps about the letter that ended it (phase 09):
    the topic, the letter's id, and — when the payload says so as a
    number — the amount, as the payload gave it (Shopify posts money as
    "1850.00"; a float here would lose that spelling). The summary sums
    it over goal_met rows; a non-numeric value is simply absent."""
    goal: dict = {"topic": event.topic, "event_id": str(event.id)}
    for key in _AMOUNT_KEYS:
        value = event.payload.get(key)
        if _as_number(value) is not None:
            goal["amount"] = value
            break
    return {"goal": goal}


def _where_matches(where: dict, payload: dict) -> bool:
    return all(payload.get(key) == value for key, value in where.items())


def _context_from_payload(payload: dict) -> dict:
    """The template-variable bridge: merchants send standard identity keys
    (customer_mobile_number, customer_name) plus whatever scalar keys
    their call template references ({item}, {cart_value}); those small
    facts ride context -> the lead payload -> template resolution.

    The walker's bookkeeping names (nodes.py's ONE definition: pointers,
    the phone, lead_*/message_*/reply_*, the repeat lists) are skipped: a
    producer key spelled `repeat_items` or `source_event_id` would corrupt
    the accumulate branch or the founding-event dedupe — the phone is
    re-added below, normalized, from what identity resolved on."""
    context = {}
    for key, value in payload.items():
        if key in _BOOKKEEPING_KEYS or key.startswith(_BOOKKEEPING_PREFIXES):
            continue  # ours to write, never a producer's
        if not isinstance(value, (str, int, float, bool)):
            continue  # nested objects/lists stay on the event row
        if len(str(value)) > _CONTEXT_VALUE_MAX_CHARS:
            continue
        context[key] = value
    return context


async def _try_enrol(
    flow: Workflow,
    definition: WorkflowDefinition,
    door: WorkflowEntryAt,
    event: RawEvent,
    customer_id: str,
    handles: Optional[dict] = None,
    open_runs: Sequence[EnrollmentRun] = (),
) -> None:
    admit, enrollment_key = _enrollment_key(door, event, str(flow.id))
    if not admit:
        return  # a keyed plan without its key: a refusal, not an error
    context = _context_from_payload(event.payload)
    context["source_event_id"] = str(event.id)
    # When the founding letter HAPPENED (its own claim, else the envelope's
    # receipt): goals compare against this, not the row's insert time (G7).
    context["entered_event_at"] = (event.occurred_at or event.received_at).isoformat()
    phone = (handles or {}).get("phone") or _phone_from_payload(event.payload)
    if phone:
        context["phone"] = phone
    run = await enrol(
        merchant_id=event.merchant_id,
        workflow=flow,
        customer_id=customer_id,
        context=context,
        enrollment_key=enrollment_key,
        door=door,
    )
    if run is None:
        # Refused — most often because a run for this key is already open.
        # THAT run's repeat words decide what it does with the repeat
        # (repeat.py; its own version's, phase 13); the UPDATE's WHERE
        # makes every other refusal a no-op, so no second signal from
        # enrol() is needed. The repeat is offered the SAME small facts
        # enrol() was — the normalized phone included, so a corrected
        # number reaches the run — minus the founding pointers
        # (_FOUNDING_KEYS): the id is what patch_open_run_query refuses
        # the founding event by, the time is what goals are measured from.
        key = enrollment_key or customer_id
        if await _answered_by(open_runs, flow, key, event):
            logger.info(
                f"run for {key} on {flow.id}: {event.topic} is its square's answer "
                f"— moved, not a repeat (event {event.id})"
            )
            return
        repeat_facts = {k: v for k, v in context.items() if k not in _FOUNDING_KEYS}
        await apply_repeat(
            event.merchant_id,
            str(flow.id),
            key,
            await _repeat_door(open_runs, flow, key, door.topic, door),
            str(event.id),
            repeat_facts,
        )


async def _repeat_door(
    open_runs: Sequence[EnrollmentRun],
    flow: Workflow,
    enrollment_key: str,
    topic: str,
    latest: WorkflowEntryAt,
) -> WorkflowEntryAt:
    """The repeat's words are the OPEN RUN'S version's door: its on_repeat
    and debounce, and the square it starts on — v5 may have renamed the
    start square, and the patch's `current_node = start` guard would then
    never find a v3 run. That version's door for this topic; if it has
    none (the door is newer than the run), its first door — the patch's
    guard makes a wrong square a no-op, never a wrong write. The run was
    read at the top of this pass; when it is not among her open runs (a
    keyed run that resolved to another customer, or a sibling tick opened
    it after the read) the latest door stands in — exactly the
    pre-pinning behaviour."""
    for run in open_runs:
        if (
            str(run.workflow_id) == str(flow.id)
            and run.enrollment_key == enrollment_key
        ):
            pinned = await definition_for(run)
            if pinned is not None:
                doors = pinned.entries
                return next((d for d in doors if d.topic == topic), doors[0])
            break
    return latest


def _enrollment_key(
    door: WorkflowEntry, event: RawEvent, workflow_id: str
) -> Tuple[bool, Optional[str]]:
    """PURE decide: (admit, key). No door.key -> (True, None): enrol()
    falls back to the customer id, canon's default. door.key set -> the
    payload's value for it. The author declared runs are per <field>; an
    event without that field cannot honestly start one, so it is refused
    like any other admission miss — never silently re-keyed to the
    customer, which would coalesce what the author said to keep apart."""
    field = door.key
    if not field:
        return True, None
    value = event.payload.get(field)
    if value in (None, ""):
        logger.info(
            f"enrol skipped: entry.key {field!r} missing in payload "
            f"(workflow {workflow_id}, event {event.id})"
        )
        return False, None
    return True, str(value)
