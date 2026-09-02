"""The entry-rules consumer (W4 + W5) — outreach's subscription on the
event spine: an attributed event whose topic matches a LIVE plan's entry
starts a run (enrol()); one matching a plan's goal ends open runs
(goal-cancel); one a wait_event node listens for wakes the run standing
on that node with the answer written in its context (W5).

A consumer is a function, not a loop (worker-runtime.md): the event
worker's pass calls consume_attributed_event() per row, inside that row's
savepoint, before its stamp. A raise here rolls back only that row, which
stays pending and returns next poll. Our writes commit on their own
(enrol() opens its own atom), so a replay is safe by idempotency — the
source-event check and the open-run unique — not by that rollback.
"""

from typing import List, Optional, Tuple

from app.core.logger import logger
from app.crm.outreach.db import accessor
from app.crm.outreach.enrol import enrol
from app.crm.outreach.nodes import _BOOKKEEPING_KEYS, _BOOKKEEPING_PREFIXES
from app.crm.outreach.repeat import apply_repeat
from app.crm.outreach.schemas import Workflow, WorkflowDefinition, WorkflowNode
from app.crm.record.contracts import RawEvent
from app.crm.shared.normalize import normalize_phone

# Fallback only: where a phone hides in a payload when no extractor
# handles were passed in (the voice mirrors, which send the flat shape).
# An external door's letter arrives VERBATIM under the two-plane ruling,
# so its phone is wherever that provider puts it — which is the source's
# extractor's business, not this file's.
_PHONE_PATHS = ("customer_mobile_number", "phone")

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
    event: RawEvent, customer_id: str, handles: Optional[dict] = None
) -> None:
    """Match one just-attributed event against every live plan's entry and
    goal topics. customer_id arrives separately: the row object still
    carries the pre-stamp value.

    ``handles`` is what the source's extractor already found. Taking it
    rather than re-reading the payload keeps ONE source-aware discovery in
    the system: the number the sends dial is then the same number identity
    resolved on, so suppression matches by construction and a new source
    needs no teaching here. The payload search stays as the fallback for
    the voice mirrors, which resolve before this consumer exists."""
    flows = await accessor.live_workflows(event.merchant_id)
    entry_matches: List[Tuple[Workflow, WorkflowDefinition]] = []
    goal_matches: List[Workflow] = []
    listening: List[Tuple[Workflow, WorkflowNode]] = []
    for flow in flows:
        definition = WorkflowDefinition.model_validate(flow.definition)
        if definition.entry.topic == event.topic and _where_matches(
            definition.entry.where, event.payload
        ):
            entry_matches.append((flow, definition))
        if event.topic in definition.goal.topics:
            goal_matches.append(flow)
        for node in definition.nodes:
            if node.type == "wait_event" and event.topic in node.topics:
                listening.append((flow, node))

    # Goal first: an order arriving right behind its checkout must not
    # cancel the run that checkout is about to start. Then replies, then
    # entries. Time-aware: only runs that began before the goal event end
    # (decisions §5) — a stale goal redelivered by the spine cannot end a
    # run born after it.
    for flow in goal_matches:
        await accessor.cancel_open_runs(
            event.merchant_id,
            str(flow.id),
            customer_id,
            "goal_met",
            event.occurred_at,
        )
    for flow, node in listening:
        answer = event.payload.get(node.key or "")
        if answer is None:
            # B1 (rollout phase 01): no key, no answer to branch on. Waking
            # the run with {reply_<node>: None} made pick_next read "the
            # alarm fired" and take the timeout edge at once — any letter
            # on the listened topic without the key ended the listening
            # window early. The window simply continues; only the alarm
            # may time it out.
            logger.info(
                f"wait_event reply ignored: key {node.key!r} missing "
                f"(workflow {flow.id}, event {event.id})"
            )
            continue
        await accessor.resume_run_on_event(
            event.merchant_id,
            str(flow.id),
            customer_id,
            node.id,
            {reply_key(node.id): str(answer)},
        )
    for flow, definition in entry_matches:
        await _try_enrol(flow, definition, event, customer_id, handles)


def reply_key(node_id: str) -> str:
    """Where a wait_event node's answer lives in the run's context."""
    return f"reply_{node_id}"


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
    event: RawEvent,
    customer_id: str,
    handles: Optional[dict] = None,
) -> None:
    admit, enrollment_key = _enrollment_key(definition, event, str(flow.id))
    if not admit:
        return  # a keyed plan without its key: a refusal, not an error
    context = _context_from_payload(event.payload)
    context["source_event_id"] = str(event.id)
    phone = (handles or {}).get("phone") or _phone_from_payload(event.payload)
    if phone:
        context["phone"] = phone
    run = await enrol(
        merchant_id=event.merchant_id,
        workflow=flow,
        customer_id=customer_id,
        context=context,
        enrollment_key=enrollment_key,
    )
    if run is None:
        # Refused — most often because a run for this key is already open.
        # The plan's repeat words decide what that open run does with the
        # repeat (repeat.py); the UPDATE's WHERE makes every other refusal
        # a no-op, so no second signal from enrol() is needed. The repeat
        # is offered the SAME small facts enrol() was — the normalized
        # phone included, so a corrected number reaches the run — minus
        # source_event_id: that pointer stays the founding event's, and
        # patch_open_run_query refuses the founding event by it.
        repeat_facts = {k: v for k, v in context.items() if k != "source_event_id"}
        await apply_repeat(
            event.merchant_id,
            str(flow.id),
            enrollment_key or customer_id,
            definition,
            str(event.id),
            repeat_facts,
        )


def _enrollment_key(
    definition: WorkflowDefinition, event: RawEvent, workflow_id: str
) -> Tuple[bool, Optional[str]]:
    """PURE decide: (admit, key). No entry.key -> (True, None): enrol()
    falls back to the customer id, canon's default. entry.key set -> the
    payload's value for it. The author declared runs are per <field>; an
    event without that field cannot honestly start one, so it is refused
    like any other admission miss — never silently re-keyed to the
    customer, which would coalesce what the author said to keep apart."""
    field = definition.entry.key
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
