"""The entry-rules consumer (W4 + W5) — outreach's subscription on the
event spine. One consumer, two reads (ADR 0023 §4; the sentence in
docs/crm/workflow-rollout/context/reading-notes.md §15.3):

  1. HER OPEN RUNS, each judged by the version it entered under
     (definitions.py): a goal tier of THAT document ends the run
     (goal-cancel); a listening wait of THAT document wakes it with
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

from typing import Any, Dict, Optional, Sequence, Tuple

from app.core.config.dynamic import CRM_CONTEXT_VALUE_MAX_CHARS
from app.core.logger import logger
from app.crm.outreach.db.accessors import (
    enrollment as enrollment_accessor,
    workflow as workflow_accessor,
)
from app.crm.outreach.definitions import definition_for
from app.crm.outreach.enrol import LOG_COMPONENT as ENROL_LOG_COMPONENT, enrol
from app.crm.outreach.nodes import awaits, listens
from app.crm.outreach.nodes.call import (
    CALL_COMPLETED,
    awaiting_key,
    cancel_queued_calls,
)
from app.crm.outreach.nodes.context import (
    CUT_SHORT_BY_KEY,
    LATEST_LETTER_KEY,
    is_bookkeeping,
    reply_key,
)
from app.crm.outreach.nodes.wait import TOPIC_KEY
from app.crm.outreach.repeat import _as_number, apply_repeat
from app.crm.outreach.reply_attribution import Addressed, addressed_run
from app.crm.outreach.schemas import (
    EnrollmentRun,
    Workflow,
    WorkflowDefinition,
    WorkflowEntry,
    WorkflowEntryAt,
    WorkflowNode,
)
from app.crm.outreach.steps import as_rows, closing
from app.crm.record.contracts import (
    CALL_REPORT_SOURCES,
    RawEvent,
    canonical_path,
    derive_for,
    field_value,
    list_values,
)
from app.crm.shared.normalize import normalize_phone
from app.crm.shared.predicate import matches

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
    event: RawEvent,
    customer_id: Optional[str],
    handles: Optional[dict] = None,
    variables: Optional[Dict[str, Any]] = None,
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
    the voice mirrors, which resolve before this consumer exists.

    ``variables`` are the template fill-ins the catalog declared for this
    (source, topic), resolved by the same engine — nested paths and derived
    names (Shopify's customer_name) that the top-level scalar copy below
    can never see."""
    if customer_id is None:
        return  # not about a person: nothing to admit, end or wake
    open_runs = await enrollment_accessor.open_runs_for_customer(
        event.merchant_id, customer_id
    )
    goal_patch = _goal_patch(event) if open_runs else None
    # Whose send this letter answers, resolved ONCE for the letter rather
    # than once per run: a reply is addressed, not matched (see
    # reply_attribution). None for anything that answers nothing of ours,
    # and then every square below behaves as it always did.
    addressed = await addressed_run(event) if open_runs else None
    if addressed is not None and addressed.run_id not in {str(r.id) for r in open_runs}:
        # She answered a run that has since ended (its goal fired, it timed
        # out, she was ejected). Narrowing to a run nobody is holding would
        # silence the letter completely, so it is treated as unaddressed and
        # her other runs hear it exactly as they did before — the answer is
        # late, not misdirected.
        addressed = None
    for run in open_runs:
        definition = await definition_for(run)
        if definition is None:
            # No version row for the pin: nothing honest can be judged for
            # this run here; the walker parks it at its next claim. Fields,
            # because nothing else says this run can no longer hear its goal.
            logger.bind(
                component=ENROL_LOG_COMPONENT,
                merchant_id=run.merchant_id,
                workflow_id=str(run.workflow_id),
                run_id=str(run.id),
                ignored_reason="definition_missing",
            ).warning(
                f"run {run.id}: definition v{run.workflow_version} missing — "
                f"goals and listening not judged (event {event.id})"
            )
            continue
        if await _end_on_goal(run, definition, event, goal_patch):
            continue  # exited: there is nothing left to wake
        await _wake_on_reply(run, definition, event, variables, addressed)

    flows = await workflow_accessor.live_workflows(event.merchant_id)
    for flow in flows:
        definition = WorkflowDefinition.model_validate(flow.definition)
        for door in definition.entries:
            if door.topic == event.topic and _where_matches(door, event):
                await _try_enrol(
                    flow,
                    definition,
                    door,
                    event,
                    customer_id,
                    handles,
                    open_runs,
                    variables,
                    addressed,
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
            # Trap 3 (canon T26): this is the ONE writer outside the walker
            # that ends a run — no claim, no visit, no flush of its own.
            # Without this every CONVERTED run, the ones that matter most,
            # would lose its final square. The letter in hand is the one
            # that cut that square short — it is named, or the pointer
            # column would lie exactly where a merchant looks first.
            steps=as_rows(
                closing(run, definition, tier.exit_reason, cut_short_by=str(event.id))
            ),
        ):
            # The event side ends the HAPPY runs; counting only the
            # walker's exits would read every plan as all timeouts.
            logger.bind(
                component=ENROL_LOG_COMPONENT,
                merchant_id=run.merchant_id,
                workflow_id=str(run.workflow_id),
                run_id=str(run.id),
                exit_reason=tier.exit_reason,
                topic=event.topic,
            ).info(
                # !r: the topic is event data; repr escapes a newline that
                # would otherwise forge a log line (CWE-117).
                f"run {run.id} exited {tier.exit_reason} on {event.topic!r}"
            )
            await cancel_queued_calls(str(run.id), f"run exited: {tier.exit_reason}")
            return True
    return False


async def _wake_on_reply(
    run: EnrollmentRun,
    definition: WorkflowDefinition,
    event: RawEvent,
    variables: Optional[Dict[str, Any]] = None,
    addressed: Optional[Addressed] = None,
) -> None:
    """A listening wait of ITS document hearing this topic wakes
    the run with the answer — the statement decides whether the token is
    standing there (a reply to a square it has left, or not yet reached,
    changes nothing), waiting or parked (phase 16: an event is evidence
    the customer moved). The letter's scalar facts ride along under the
    square (context.facts.<square>), so a later call can say what this
    stage's letter said; the same bridge enrol uses, so bookkeeping names
    and nested payload never reach the run.

    Those facts are the catalog's DECLARED variables over the top-level
    scalar copy — the same pair enrol receives. A woken square once
    re-read the raw payload by hand, so a derived or nested field reached
    a STARTING run and never a WAITING one (a WhatsApp square heard only
    `messaging_product`); one reader of one payload is the engine's whole
    point.

    Merged, not substituted — a raw scalar the catalog does not declare
    survives, and the declared name wins the tie. BOTH halves cross
    _context_from_payload, so the bookkeeping filter, the scalar filter
    and the size ceiling hold whichever door a value arrives by: a
    declared `phone` must not overwrite the number the sends dial, and a
    value a starting run would drop must not reach a waiting one.

    ``addressed`` is the run and square whose send this letter ANSWERS
    (reply_attribution, which holds the why). It narrows first and it is
    not advice; None leaves every square exactly as it was.
    """
    if addressed is not None and addressed.run_id != str(run.id):
        return  # she answered another run of hers
    max_chars = await CRM_CONTEXT_VALUE_MAX_CHARS()
    facts = {
        **_context_from_payload(event.payload, max_chars),
        **_context_from_payload(variables or {}, max_chars),
    }
    for node in definition.nodes:
        if not listens(node) or event.topic not in node.topics:
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
            logger.bind(
                component=ENROL_LOG_COMPONENT,
                merchant_id=run.merchant_id,
                workflow_id=str(run.workflow_id),
                run_id=str(run.id),
                ignored_reason="reply_key_missing",
            ).info(
                f"listening wait reply ignored: key {node.key!r} missing "
                f"(run {run.id}, event {event.id})"
            )
            continue
        # The answer, and which square heard the latest letter — so the
        # facts of THIS letter win the next call even after the run has
        # moved on (nodes.run_facts; a ladder hears a stage's letter on
        # the square it leaves). The call's own report yields to a letter
        # already on the square: a merchant letter heard while the call
        # rang (a ringing lead is not aborted) is the newer word, and the
        # report landing behind it before the walker's pass would replace
        # its answer and its facts — the letter lost, the next call built
        # from the report under the letter's pointer.
        await enrollment_accessor.resume_run_by_id(
            run.merchant_id,
            str(run.id),
            node.id,
            _reply_patch(node, event, answer),
            facts,
            unless_key=_yields_to(node, event),
        )
    # The run may be standing on a square that listens to NOTHING: the
    # door's start square before the walker's first visit (a condition, a
    # call), or an immediate square a walk parked on. Every resume above
    # then matched no row, and a second event two seconds behind the first
    # was silently lost. The letter has no square to answer, but it is
    # still the newest word: its facts refresh the run at the top level and
    # re-arm it, so the visit decides on THIS letter — the latest letter
    # decides, never an earlier one. Judged through the same lens as a
    # square's reply (a listening square of this plan would have taken it:
    # its topic, its match, its key), so a letter the squares would ignore
    # is ignored here too.
    current = next((n for n in definition.nodes if n.id == run.current_node), None)
    if current is None or listens(current):
        return
    if any(
        listens(n)
        and event.topic in n.topics
        and _is_about(n, event, run)
        and _answer_for(n, event) is not None
        for n in definition.nodes
    ):
        await enrollment_accessor.refresh_run_facts(
            run.merchant_id,
            str(run.id),
            current.id,
            facts,
            # Its own argument, never folded into `facts`: the same dict on
            # the reply path becomes context.facts.<square>, which run_facts
            # flattens into template variables (canon T26).
            cut_short_by=str(event.id),
        )


def _reply_patch(node: WorkflowNode, event: RawEvent, answer: str) -> Dict[str, str]:
    """PURE: what a heard letter writes on the run — its answer, the
    pointer to the letter that beat the alarm (canon T26: left for the
    flush that follows, which records it as this square's cut_short_by
    and reads the visit as arrived_by = letter; a pointer into
    crm_event_raw, never a photocopy), and, when the letter is the
    producer's word, the pointer that makes its facts the latest
    (latest_letter). Our own call reports (a call finished) answer their
    square and keep their facts under it (facts.<square>, readable as
    facts_<square>_<key>), but never take the pointer: they carry none of
    the merchant's facts, so a follow-up call after a quiet customer would
    be built from the founding letter alone — the offers the merchant sent
    before the first call gone from the second."""
    patch = {reply_key(node.id): answer, CUT_SHORT_BY_KEY: str(event.id)}
    if event.source not in CALL_REPORT_SOURCES:
        patch[LATEST_LETTER_KEY] = node.id
    return patch


def _yields_to(node: WorkflowNode, event: RawEvent) -> Optional[str]:
    """PURE: the context key whose presence makes this letter stand down,
    or None. Only the waiting call's own report yields — to the reply a
    merchant letter already left on the square. Every merchant letter is
    the latest word and replaces whatever is there."""
    if awaits(node) and event.topic == CALL_COMPLETED:
        return reply_key(node.id)
    return None


def _is_about(node: WorkflowNode, event: RawEvent, run: EnrollmentRun) -> bool:
    """PURE: is this letter about THIS run, as the square's `match` asks
    (phase 18)? The letter's field against the run's own id or a context
    field, as text (the goal-key precedent). No match word = every
    letter on the topic is hers; a letter without the field claims
    nobody, so it is not hers either.

    A waiting call square asks its own question of its own report: the
    call.completed whose lead_id is the lead it queued (awaiting_key),
    never any other call of hers — a late report from an earlier visit,
    after the backstop moved the square on, names a lead no square is
    waiting for and wakes nothing. Its merchant topics are judged as any
    listening square's."""
    if awaits(node) and event.topic == CALL_COMPLETED:
        claimed = event.payload.get("lead_id")
        mine = run.context.get(awaiting_key(node.id))
        return (
            claimed not in (None, "") and mine is not None and str(claimed) == str(mine)
        )
    if node.match is None:
        return True
    claimed = field_value(
        event.payload,
        canonical_path(node.match.payload),
        derive_for(event.source, event.topic),
    )
    if claimed is None:
        return False
    mine = str(run.id) if node.match.run == "id" else run.context.get(node.match.run)
    return mine is not None and str(claimed) == str(mine)


def _answer_for(node: WorkflowNode, event: RawEvent) -> Optional[str]:
    """PURE: what this letter answers on this square — the topic itself
    for a $topic square (phase 15: the branch is the letter's NAME), else
    the payload field the square branches on — resolved through the
    catalog like entry.where and entry.key, so a derived field (whatsapp's
    reply, read from messages[0].button) answers too, not only top-level
    keys; None when the square is not listening for the topic, or the
    field is missing (B1). The ONE definition of "this letter is this
    square's answer": the wake and the repeat refusal below both ask it."""
    if not listens(node) or event.topic not in node.topics:
        return None
    answer = (
        event.topic
        if node.key == TOPIC_KEY
        else field_value(
            event.payload,
            canonical_path(node.key or ""),
            derive_for(event.source, event.topic),
        )
    )
    return None if answer is None else str(answer)


async def _answered_by(
    open_runs: Sequence[EnrollmentRun],
    flow: Workflow,
    enrollment_key: str,
    event: RawEvent,
    addressed: Optional[Addressed] = None,
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
            if square is None:
                return False
            if addressed is not None and addressed.run_id != str(run.id):
                # Judged through the SAME lens the wake used, or a letter the
                # wake correctly ignored would be read as this run's answer
                # and apply_repeat would skip a repeat it owes.
                return False
            return (
                _is_about(square, event, run) and _answer_for(square, event) is not None
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


def _where_matches(door: WorkflowEntry, event: RawEvent) -> bool:
    """One door's typed where-grammar against the payload
    (shared/predicate.py); fields resolve through record's catalog paths —
    dot-walks and the code layer's derived fields. No table read: the
    validator guaranteed op-type fit at publish.

    A path that crosses an array (payload.products.sub_category) reads as
    the RAW list of what the elements hold — the engine's own list walk, no
    item_where, no rendering — so `includes "Mobile"` asks whether any
    product is a mobile, with the value written in the plan (design/
    event-catalog.md §The `list` ruling). An empty array reads as absent
    (`or None`): `exists` then does not hold on an empty basket and
    `not_exists` does — the same normalisation _element_holds applies. A
    path that crosses no array keeps its dot-walk, and a bare value where
    an array was expected reaches the evaluator as a scalar, which
    `includes` counts as a list of one."""
    derive = derive_for(event.source, event.topic)

    def lookup(path: str) -> Any:
        values = list_values(event.payload, path)
        if values is not None:
            if not values or all(v is None for v in values):
                return None
            return values
        return field_value(event.payload, path, derive)

    return matches(door.where, lookup)


def _context_from_payload(payload: dict, max_chars: int) -> dict:
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
        if is_bookkeeping(key):
            continue  # ours to write, never a producer's
        if value is not None and not isinstance(value, (str, int, float, bool)):
            continue  # nested objects/lists stay on the event row
        # None passes: it is the extractor's "declared, but this letter has
        # nothing" — the value that lets the latest letter CLEAR a fact.
        if len(str(value)) > max_chars:
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
    variables: Optional[Dict[str, Any]] = None,
    addressed: Optional[Addressed] = None,
) -> None:
    admit, enrollment_key = _enrollment_key(door, event, str(flow.id))
    if not admit:
        return  # a keyed plan without its key: a refusal, not an error
    max_chars = await CRM_CONTEXT_VALUE_MAX_CHARS()
    context = _context_from_payload(event.payload, max_chars)
    # The catalog's declared variables win over the scalar copy: the engine
    # resolved them through the declared paths (customer_name from
    # customer.first_name + last_name), and a bookkeeping name is still ours.
    context.update(_context_from_payload(variables or {}, max_chars))
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
        if await _answered_by(open_runs, flow, key, event, addressed):
            logger.bind(
                component=ENROL_LOG_COMPONENT,
                merchant_id=event.merchant_id,
                workflow_id=str(flow.id),
                topic=event.topic,
                ignored_reason="answered_wait",
            ).info(
                f"run for {key!r} on {flow.id}: {event.topic!r} is its square's "
                f"answer — moved, not a repeat (event {event.id})"
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
    value = field_value(
        event.payload, canonical_path(field), derive_for(event.source, event.topic)
    )
    if value in (None, ""):
        # The refusal that reads as silence: every event arrives, every
        # one is refused, no run starts. Shares the refusal shape so one
        # rule counts it beside the expected ones and says which grew.
        logger.bind(
            component=ENROL_LOG_COMPONENT,
            merchant_id=event.merchant_id,
            workflow_id=workflow_id,
            skip_reason="entry_key_missing",
        ).info(
            f"enrol skipped: entry.key {field!r} missing in payload "
            f"(workflow {workflow_id}, event {event.id})"
        )
        return False, None
    return True, str(value)
