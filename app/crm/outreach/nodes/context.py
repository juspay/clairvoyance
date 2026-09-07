"""The run context, read the same way by every word of the vocabulary.

One file, because these are not "shared helpers" in the bag sense — they
are the ONE answer to "what in a run's context is ours and what is the
producer's". The call payload and the send variables both derive from
``run_facts``, so they can never disagree about it; the bookkeeping keys
are the same list ``entry.py`` filters a merge against. A second copy of
either would let a walker key reach a customer's message.

Leaf inside the package: imports the schemas and nothing else from
outreach, so a word module may import it without a cycle.
"""

from typing import Any, Dict, Optional

from app.crm.outreach.schemas import WorkflowNode

# The walker's own bookkeeping in a run's context — never a template
# variable, never a lead payload key: pointers, the phone (re-added under
# its canonical key by the call node), per-node results and answers.
_BOOKKEEPING_KEYS = (
    "source_event_id",
    "entered_event_at",  # entry.py: when the founding letter happened (G7)
    "goal",  # entry.py: the letter that ended the run, and its amount (phase 09)
    "phone",
    "customer_mobile_number",
    "repeat_event_ids",  # repeat.py: which letters already patched this run
    "repeat_items",  # repeat.py: accumulate's list — never a template variable
    "facts",  # entry.py: each square's letter, by square (phase 16) — flattened below
    "latest_letter",  # entry.py: which square heard the most recent letter (phase 17)
    "current_node",  # run_facts: computed from the square, never a producer's
    "current_stage",
)
# The pointer the consumer writes with every reply (phase 17): the square
# that heard the most recent letter, so run_facts can let that letter win
# — on a ladder the letter that moves the run is heard on the square it
# LEAVES, and the action then executes as its own square, so "the current
# square's facts" would never be the latest stage's.
LATEST_LETTER_KEY = "latest_letter"
_BOOKKEEPING_PREFIXES = ("lead_", "message_", "reply_", "action_")

# The merchant's own id for the thing a call is about. Buddy's reporter
# echoes lead.request_id back to the merchant as orderId on every outcome
# webhook — nautilus matches it to the Shopify order, so it must be THEIR
# id, not ours. On a KEYED plan that is the enrollment key itself (entry.key
# names the order field — Shopify's `id`, a platform-wide unique); the flat
# keys below serve unkeyed plans, and the run id is the last fallback.
_REQUEST_ID_KEYS = ("order_id", "request_id")


def reply_key(node_id: str) -> str:
    """Where a wait_event square's answer lives in the run's context."""
    return f"reply_{node_id}"


def without_reply(context: Dict[str, Any], node_id: str) -> Dict[str, Any]:
    """PURE: the context with this square's answer cleared — written when
    the token leaves the square (phase 15). A door may start a run on any
    square, so a square can be revisited; a stale answer left behind
    would resolve the revisit at once, on the old reply."""
    return {key: value for key, value in context.items() if key != reply_key(node_id)}


def lead_request_id(
    context: Dict[str, Any], run_id: str, enrollment_key: Optional[str] = None
) -> str:
    """PURE: what this run is ABOUT, for the merchant — the enrollment key
    when the plan is keyed (the order id the author named), else the
    merchant's order/request id from the run's facts, else a traceable
    wf-<run id>."""
    if enrollment_key:
        return str(enrollment_key)
    for key in _REQUEST_ID_KEYS:
        value = context.get(key)
        if value not in (None, ""):
            return str(value)
    return f"wf-{run_id}"


def run_facts(
    context: Dict[str, Any], node: Optional[WorkflowNode] = None
) -> Dict[str, Any]:
    """PURE: the run's small facts = context minus the walker's own
    bookkeeping. The ONE filter both the call payload and the send
    variables derive from, so they can never disagree on what is ours.

    Phase 16: each square's letter lives under context.facts.<square>
    (entry.py writes it on resume). Flattened here for templates: the
    top-level facts first, then the LATEST letter's override them (the
    square that heard it is context.latest_letter, phase 17 — the most
    recent stage wins the call), then the CURRENT square's own, and every
    square's stay reachable as facts_<square>_<key>. With the square
    given, current_node (and current_stage when the square is labelled)
    ride along, so one call template can say "you stopped at
    {current_stage}"."""
    facts = {
        key: value
        for key, value in context.items()
        if key not in _BOOKKEEPING_KEYS and not key.startswith(_BOOKKEEPING_PREFIXES)
    }
    by_square = context.get("facts")
    by_square = by_square if isinstance(by_square, dict) else {}
    for square, letter in by_square.items():
        if isinstance(letter, dict):
            for key, value in letter.items():
                facts[f"facts_{square}_{key}"] = value
    latest = context.get(LATEST_LETTER_KEY)
    if isinstance(latest, str) and isinstance(by_square.get(latest), dict):
        facts.update(by_square[latest])
    if node is not None:
        current = by_square.get(node.id)
        if isinstance(current, dict):
            facts.update(current)
        facts["current_node"] = node.id
        if node.stage:
            facts["current_stage"] = node.stage
    return facts


def send_variables(
    mapping: Dict[str, str],
    context: Dict[str, Any],
    node: Optional[WorkflowNode] = None,
) -> Dict[str, Any]:
    """PURE: the template's fill-ins = EXACTLY the facts the send node
    mapped, {blank: facts[fact]} over run_facts (so a blank may name a
    stage's letter, facts_<square>_<key>, or current_node/current_stage)
    — no map, no parameters. Bookkeeping is never mapped: the validator
    only admits declared names on the right-hand side.

    Two honest refusals, both parking the run rather than posting a
    half-filled or misspelled message: a mapped fact absent from the run
    (KeyError(fact)), and a mapped value a provider cannot render — a
    bool, a None, a list (ValueError naming the fact): "True" or "None"
    inside a customer's message is corruption that looks delivered."""
    facts = run_facts(context, node)
    variables: Dict[str, Any] = {}
    for blank, fact in mapping.items():
        value = facts[fact]
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError(
                f"mapped fact {fact!r} is {type(value).__name__}, not text — "
                "a template blank needs text or a number"
            )
        variables[blank] = value
    return variables
